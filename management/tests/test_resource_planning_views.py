"""
View tests for resource planning CBVs and schedule.

Covers:
- ResourcePlanningAddView: GET pre-population, POST individual / grouped / multi-individual
- ResourcePlanningGroupChangeView: GET initial values (localtime, exclude_weekends),
  POST update / dissolve / cancel / _addanother
- ResourcePlanningIndividualChangeView: GET initial values, POST single update /
  convert to group / bulk individual / _continue / _addanother
- Schedule: exclude_weekends hides planning on weekends
"""
import datetime

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, Client as HttpClient, override_settings
from django.urls import reverse
from django.utils import timezone

from company.models import Company, Employee
from management.models import (
    ProjectFile,
    ResourcePlanning,
    ResourcePlanningGroup,
    ResourcePlanningStatus,
    ResourcePlanningType,
)
from management.services.resource_planning import create_plannings
from sales.models import Client

User = get_user_model()

# ManifestStaticFilesStorage (production / DEBUG=False) needs collectstatic; admin
# view tests render templates with {% static %} tags — use plain storage in tests.
_PLANNING_VIEW_STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}


# ── Shared base ───────────────────────────────────────────────────────────────

@override_settings(STORAGES=_PLANNING_VIEW_STORAGES)
class _ViewBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="View Co")
        cls.client_obj = Client.objects.create(company=cls.company, name="Client")

        _hired = datetime.date(2020, 1, 1)
        cls.employee = Employee.objects.create(
            company=cls.company, first_name="Ana", last_name="García",
            hired_at=_hired, is_active=True,
        )
        cls.employee2 = Employee.objects.create(
            company=cls.company, first_name="Luis", last_name="Pérez",
            hired_at=_hired, is_active=True,
        )

        cls.project_file = ProjectFile.objects.create(
            company=cls.company, reference="EXP-00001", name="Project",
            status="open", client=cls.client_obj,
        )
        cls.planning_type = ResourcePlanningType.objects.create(
            company=cls.company, name="Install", color="#000000",
        )

        cls.superuser = User.objects.create_superuser(
            username="admin", password="admin", email="admin@test.com"
        )

        cls.employee_ct = ContentType.objects.get(app_label="company", model="employee")

    def setUp(self):
        self.http = HttpClient()
        self.http.force_login(self.superuser)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _post_data(self, **overrides):
        data = {
            "company": self.company.pk,
            "project_file": self.project_file.pk,
            "planning_type": self.planning_type.pk,
            "status": ResourcePlanningStatus.PLANNED,
            "resources": f"employee:{self.employee.pk}",
            "date_from": "2026-07-07",
            "date_to": "2026-07-09",
            "daily_start": "08:00:00",
            "daily_end": "17:00:00",
            "_save": "1",
        }
        data.update(overrides)
        return data

    def _make_group(self, resources=None, date_from=None, date_to=None,
                    exclude_weekends=False):
        if resources is None:
            resources = [
                (self.employee_ct, self.employee.pk),
                (self.employee_ct, self.employee2.pk),
            ]
        return create_plannings(
            company=self.company,
            project_file=self.project_file,
            planning_type=self.planning_type,
            status=ResourcePlanningStatus.PLANNED,
            notes="",
            resources=resources,
            date_from=date_from or datetime.date(2026, 7, 7),
            date_to=date_to or datetime.date(2026, 7, 9),
            daily_start=datetime.time(8, 0),
            daily_end=datetime.time(17, 0),
            exclude_weekends=exclude_weekends,
            is_group=True,
        )

    def _make_individual(self, date_from=None, date_to=None, exclude_weekends=False):
        plannings = create_plannings(
            company=self.company,
            project_file=self.project_file,
            planning_type=self.planning_type,
            status=ResourcePlanningStatus.PLANNED,
            notes="",
            resources=[(self.employee_ct, self.employee.pk)],
            date_from=date_from or datetime.date(2026, 7, 7),
            date_to=date_to or datetime.date(2026, 7, 9),
            daily_start=datetime.time(8, 0),
            daily_end=datetime.time(17, 0),
            exclude_weekends=exclude_weekends,
            is_group=False,
        )
        return plannings[0]

    # ── URL helpers ───────────────────────────────────────────────────────────

    @property
    def add_url(self):
        return reverse("admin:management_resourceplanning_bulk_add")

    def group_url(self, pk):
        return reverse("admin:management_resourceplanning_group_change", args=[pk])

    def individual_url(self, pk):
        return reverse("admin:management_resourceplanning_individual_change", args=[pk])


# ── Add view ──────────────────────────────────────────────────────────────────

class TestAddViewGet(_ViewBase):
    def test_get_renders_form(self):
        r = self.http.get(self.add_url)
        self.assertEqual(r.status_code, 200)

    def test_get_prepopulates_resources(self):
        r = self.http.get(self.add_url + f"?resources=employee:{self.employee.pk}")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, f"employee:{self.employee.pk}")

    def test_get_prepopulates_day(self):
        r = self.http.get(self.add_url + "?day=2026-07-07")
        self.assertEqual(r.status_code, 200)
        form = r.context["form"]
        self.assertEqual(form.initial.get("date_from"), datetime.date(2026, 7, 7))
        self.assertEqual(form.initial.get("date_to"), datetime.date(2026, 7, 7))


class TestAddViewPost(_ViewBase):
    def test_post_creates_individual_planning(self):
        r = self.http.post(self.add_url, self._post_data())
        self.assertEqual(ResourcePlanning.objects.count(), 1)
        self.assertIsNone(ResourcePlanning.objects.get().group_id)

    def test_post_creates_grouped_plannings(self):
        data = self._post_data(
            resources=f"employee:{self.employee.pk},employee:{self.employee2.pk}",
            is_group="on",
        )
        self.http.post(self.add_url, data)
        self.assertEqual(ResourcePlanning.objects.count(), 2)
        self.assertEqual(ResourcePlanningGroup.objects.count(), 1)
        group_ids = set(ResourcePlanning.objects.values_list("group_id", flat=True))
        self.assertEqual(len(group_ids), 1)

    def test_post_multi_resource_no_group_creates_individuals(self):
        data = self._post_data(
            resources=f"employee:{self.employee.pk},employee:{self.employee2.pk}",
        )
        self.http.post(self.add_url, data)
        self.assertEqual(ResourcePlanning.objects.count(), 2)
        self.assertEqual(ResourcePlanningGroup.objects.count(), 0)

    def test_post_save_redirects_to_changelist(self):
        r = self.http.post(self.add_url, self._post_data())
        self.assertRedirects(
            r, reverse("admin:management_resourceplanning_changelist"),
            fetch_redirect_response=False,
        )

    def test_post_addanother_redirects_to_add_keeping_shared_fields(self):
        data = self._post_data(exclude_weekends="on")
        data.pop("_save")
        data["_addanother"] = "1"
        r = self.http.post(self.add_url, data)
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.url.startswith(self.add_url + "?"))
        for fragment in (
            f"company={self.company.pk}",
            f"resources=employee%3A{self.employee.pk}",
            "date_from=2026-07-07",
            "date_to=2026-07-09",
            "daily_start=08%3A00%3A00",
            "daily_end=17%3A00%3A00",
            "exclude_weekends=1",
        ):
            self.assertIn(fragment, r.url)

    def test_get_prepopulates_shared_fields_from_addanother(self):
        r = self.http.get(self.add_url + (
            f"?company={self.company.pk}"
            f"&resources=employee:{self.employee.pk}"
            "&date_from=2026-07-07&date_to=2026-07-09"
            "&daily_start=08:00:00&daily_end=17:00:00"
            "&exclude_weekends=0"
        ))
        self.assertEqual(r.status_code, 200)
        initial = r.context["form"].initial
        self.assertEqual(initial["company"], str(self.company.pk))
        self.assertEqual(initial["resources"], f"employee:{self.employee.pk}")
        self.assertEqual(initial["date_from"], datetime.date(2026, 7, 7))
        self.assertEqual(initial["date_to"], datetime.date(2026, 7, 9))
        self.assertEqual(initial["daily_start"], datetime.time(8, 0))
        self.assertEqual(initial["daily_end"], datetime.time(17, 0))
        self.assertFalse(initial["exclude_weekends"])

    def test_post_grouped_save_redirects_to_group_change(self):
        data = self._post_data(
            resources=f"employee:{self.employee.pk},employee:{self.employee2.pk}",
            is_group="on",
        )
        r = self.http.post(self.add_url, data)
        group = ResourcePlanningGroup.objects.get()
        self.assertRedirects(
            r, self.group_url(group.pk), fetch_redirect_response=False
        )

    def test_post_invalid_shows_errors(self):
        data = self._post_data(
            date_from="2026-07-07",
            date_to="2026-07-07",
            daily_start="17:00:00",
            daily_end="08:00:00",
        )
        r = self.http.post(self.add_url, data)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(ResourcePlanning.objects.count(), 0)

    def test_post_spanish_date_format_multi_day(self):
        data = self._post_data(
            date_from="07/07/2026",
            date_to="09/07/2026",
            daily_start="08:00",
            daily_end="17:00",
        )
        r = self.http.post(self.add_url, data)
        self.assertEqual(ResourcePlanning.objects.count(), 1)
        p = ResourcePlanning.objects.get()
        local_end = timezone.localtime(p.end)
        self.assertEqual(local_end.date(), datetime.date(2026, 7, 9))

    def test_post_exclude_weekends_saved(self):
        self.http.post(self.add_url, self._post_data(exclude_weekends="on"))
        self.assertTrue(ResourcePlanning.objects.get().exclude_weekends)

    def test_post_exclude_weekends_unchecked_saved_false(self):
        self.http.post(self.add_url, self._post_data())
        self.assertFalse(ResourcePlanning.objects.get().exclude_weekends)


# ── Group change view ─────────────────────────────────────────────────────────

class TestGroupChangeViewGet(_ViewBase):
    def test_get_renders_form(self):
        plannings = self._make_group()
        group = plannings[0].group
        r = self.http.get(self.group_url(group.pk))
        self.assertEqual(r.status_code, 200)

    def test_get_initial_exclude_weekends_false_when_has_weekends(self):
        # Plannings spanning Sat/Sun → exclude_weekends detected as False
        plannings = self._make_group(
            date_from=datetime.date(2026, 7, 6),   # Monday
            date_to=datetime.date(2026, 7, 12),    # Sunday
            exclude_weekends=False,
        )
        group = plannings[0].group
        r = self.http.get(self.group_url(group.pk))
        self.assertEqual(r.status_code, 200)
        form = r.context["form"]
        self.assertFalse(form.initial.get("exclude_weekends"))

    def test_get_initial_exclude_weekends_true_when_no_weekends(self):
        # Plannings Mon–Thu with exclude_weekends=True → detected as True
        plannings = self._make_group(
            date_from=datetime.date(2026, 7, 7),   # Tuesday
            date_to=datetime.date(2026, 7, 9),     # Thursday
            exclude_weekends=True,
        )
        group = plannings[0].group
        r = self.http.get(self.group_url(group.pk))
        self.assertEqual(r.status_code, 200)
        form = r.context["form"]
        self.assertTrue(form.initial.get("exclude_weekends"))

    def test_get_initial_date_to_uses_end_date(self):
        plannings = self._make_group(
            date_from=datetime.date(2026, 7, 7),
            date_to=datetime.date(2026, 7, 9),
        )
        group = plannings[0].group
        r = self.http.get(self.group_url(group.pk))
        form = r.context["form"]
        self.assertEqual(form.initial["date_to"], datetime.date(2026, 7, 9))
        self.assertEqual(form.initial["daily_end"], datetime.time(17, 0))


class TestGroupChangeViewPost(_ViewBase):
    def setUp(self):
        super().setUp()
        self.plannings = self._make_group()
        self.group = self.plannings[0].group

    def _group_post(self, **overrides):
        data = self._post_data(
            resources=f"employee:{self.employee.pk},employee:{self.employee2.pk}",
            is_group="on",
        )
        data.update(overrides)
        return self.http.post(self.group_url(self.group.pk), data)

    def test_post_updates_group_plannings(self):
        old_pks = set(ResourcePlanning.objects.values_list("pk", flat=True))
        self._group_post()
        new_pks = set(ResourcePlanning.objects.values_list("pk", flat=True))
        self.assertFalse(old_pks & new_pks)
        self.assertEqual(ResourcePlanning.objects.count(), 2)

    def test_post_redirects_back_to_group(self):
        r = self._group_post()
        self.assertRedirects(
            r, self.group_url(self.group.pk), fetch_redirect_response=False
        )

    def test_post_addanother_redirects_to_add(self):
        r = self._group_post(**{"_addanother": "1", "_save": ""})
        self.assertRedirects(r, self.add_url, fetch_redirect_response=False)

    def test_post_dissolve_when_is_group_unchecked(self):
        data = self._post_data(
            resources=f"employee:{self.employee.pk},employee:{self.employee2.pk}",
        )
        self.http.post(self.group_url(self.group.pk), data)
        self.assertFalse(ResourcePlanningGroup.objects.filter(pk=self.group.pk).exists())
        self.assertEqual(ResourcePlanning.objects.filter(group__isnull=True).count(), 2)

    def test_post_dissolve_redirects_to_changelist(self):
        data = self._post_data(
            resources=f"employee:{self.employee.pk},employee:{self.employee2.pk}",
        )
        r = self.http.post(self.group_url(self.group.pk), data)
        self.assertRedirects(
            r, reverse("admin:management_resourceplanning_changelist"),
            fetch_redirect_response=False,
        )

    def test_post_cancel_group_cancels_all(self):
        self.http.post(self.group_url(self.group.pk), {"cancel_group": "1"})
        statuses = set(ResourcePlanning.objects.values_list("status", flat=True))
        self.assertEqual(statuses, {ResourcePlanningStatus.CANCELLED})

    def test_post_exclude_weekends_persisted(self):
        self._group_post(exclude_weekends="on")
        self.assertTrue(
            all(ResourcePlanning.objects.values_list("exclude_weekends", flat=True))
        )


# ── Individual change view ────────────────────────────────────────────────────

class TestIndividualChangeViewGet(_ViewBase):
    def test_get_renders_form(self):
        p = self._make_individual()
        r = self.http.get(self.individual_url(p.pk))
        self.assertEqual(r.status_code, 200)

    def test_get_initial_dates_use_local_time(self):
        # Planning stored as UTC — form initial must use local Madrid time, not UTC
        p = self._make_individual(
            date_from=datetime.date(2026, 7, 7),
            date_to=datetime.date(2026, 7, 9),
        )
        r = self.http.get(self.individual_url(p.pk))
        self.assertEqual(r.status_code, 200)
        form = r.context["form"]
        local_start = timezone.localtime(p.start)
        self.assertEqual(form.initial["date_from"], local_start.date())
        self.assertEqual(form.initial["daily_start"], local_start.time())

    def test_get_initial_date_to_uses_end_date(self):
        # Multi-day planning: date_to must reflect end date, not start date
        p = self._make_individual(
            date_from=datetime.date(2026, 7, 7),
            date_to=datetime.date(2026, 7, 9),
        )
        r = self.http.get(self.individual_url(p.pk))
        local_end = timezone.localtime(p.end)
        form = r.context["form"]
        self.assertEqual(form.initial["date_to"], local_end.date())

    def test_get_initial_exclude_weekends_reflects_model(self):
        p = self._make_individual(exclude_weekends=True)
        r = self.http.get(self.individual_url(p.pk))
        form = r.context["form"]
        self.assertTrue(form.initial.get("exclude_weekends"))

    def test_get_shows_delete_button(self):
        p = self._make_individual()
        r = self.http.get(self.individual_url(p.pk))
        delete_url = reverse("admin:management_resourceplanning_delete", args=[p.pk])
        self.assertContains(r, delete_url)


class TestIndividualChangeViewPost(_ViewBase):
    def setUp(self):
        super().setUp()
        self.planning = self._make_individual()

    def _ind_post(self, **overrides):
        data = self._post_data()
        data.update(overrides)
        return self.http.post(self.individual_url(self.planning.pk), data)

    def test_post_updates_notes(self):
        self._ind_post(notes="updated note")
        self.assertEqual(ResourcePlanning.objects.get(pk=self.planning.pk).notes, "updated note")

    def test_post_updates_status(self):
        self._ind_post(status=ResourcePlanningStatus.CONFIRMED)
        self.assertEqual(
            ResourcePlanning.objects.get(pk=self.planning.pk).status,
            ResourcePlanningStatus.CONFIRMED,
        )

    def test_post_updates_exclude_weekends(self):
        self._ind_post(exclude_weekends="on")
        self.assertTrue(ResourcePlanning.objects.get(pk=self.planning.pk).exclude_weekends)

    def test_post_updates_date_range(self):
        self._ind_post(date_from="2026-07-14", date_to="2026-07-16")
        p = ResourcePlanning.objects.get(pk=self.planning.pk)
        local_start = timezone.localtime(p.start)
        local_end   = timezone.localtime(p.end)
        self.assertEqual(local_start.date(), datetime.date(2026, 7, 14))
        self.assertEqual(local_end.date(), datetime.date(2026, 7, 16))

    def test_post_save_redirects_to_changelist(self):
        r = self._ind_post()
        self.assertRedirects(
            r, reverse("admin:management_resourceplanning_changelist"),
            fetch_redirect_response=False,
        )

    def test_post_continue_redirects_back(self):
        data = self._post_data(**{"_continue": "1"})
        r = self.http.post(self.individual_url(self.planning.pk), data)
        self.assertRedirects(
            r, self.individual_url(self.planning.pk), fetch_redirect_response=False
        )

    def test_post_addanother_redirects_to_add(self):
        data = self._post_data(**{"_addanother": "1"})
        r = self.http.post(self.individual_url(self.planning.pk), data)
        self.assertRedirects(r, self.add_url, fetch_redirect_response=False)

    def test_post_multi_resource_with_group_converts_to_group(self):
        data = self._post_data(
            resources=f"employee:{self.employee.pk},employee:{self.employee2.pk}",
            is_group="on",
        )
        self.http.post(self.individual_url(self.planning.pk), data)
        self.assertFalse(ResourcePlanning.objects.filter(pk=self.planning.pk).exists())
        self.assertEqual(ResourcePlanningGroup.objects.count(), 1)
        self.assertEqual(ResourcePlanning.objects.count(), 2)

    def test_post_multi_resource_no_group_creates_individuals(self):
        data = self._post_data(
            resources=f"employee:{self.employee.pk},employee:{self.employee2.pk}",
        )
        self.http.post(self.individual_url(self.planning.pk), data)
        self.assertFalse(ResourcePlanning.objects.filter(pk=self.planning.pk).exists())
        self.assertEqual(ResourcePlanning.objects.count(), 2)
        self.assertEqual(ResourcePlanningGroup.objects.count(), 0)

    def test_post_convert_to_group_redirects_to_group(self):
        data = self._post_data(
            resources=f"employee:{self.employee.pk},employee:{self.employee2.pk}",
            is_group="on",
        )
        r = self.http.post(self.individual_url(self.planning.pk), data)
        group = ResourcePlanningGroup.objects.get()
        self.assertRedirects(
            r, self.group_url(group.pk), fetch_redirect_response=False
        )


# ── Schedule: exclude_weekends filtering ──────────────────────────────────────

class TestScheduleExcludeWeekends(_ViewBase):
    @property
    def schedule_url(self):
        return reverse("admin:management_resourceplanning_schedule")

    def test_planning_with_exclude_weekends_hidden_on_saturday(self):
        # Create a planning Mon–Sun with exclude_weekends=True
        create_plannings(
            company=self.company,
            project_file=self.project_file,
            planning_type=self.planning_type,
            status=ResourcePlanningStatus.PLANNED,
            notes="",
            resources=[(self.employee_ct, self.employee.pk)],
            date_from=datetime.date(2026, 7, 6),   # Monday
            date_to=datetime.date(2026, 7, 12),    # Sunday
            daily_start=datetime.time(8, 0),
            daily_end=datetime.time(17, 0),
            exclude_weekends=True,
            is_group=False,
        )
        # View the week that includes Sat 11 Jul
        r = self.http.get(
            self.schedule_url + "?day=2026-07-06&num_days=7"
            + f"&company={self.company.pk}"
        )
        self.assertEqual(r.status_code, 200)
        # The planning block links to the change URL — check it appears on Mon but
        # the schedule logic skips Sat/Sun (no block rendered for those days).
        # We verify by checking context resource_groups has empty cells for Sat/Sun.
        resource_groups = r.context["resource_groups"]
        self.assertTrue(len(resource_groups) > 0)
        cells = resource_groups[0]["resources"][0]["cells"]
        sat = next(c for c in cells if c["date_key"] == "2026-07-11")
        sun = next(c for c in cells if c["date_key"] == "2026-07-12")
        mon = next(c for c in cells if c["date_key"] == "2026-07-06")
        self.assertEqual(sat["blocks"], [], "Saturday should have no blocks")
        self.assertEqual(sun["blocks"], [], "Sunday should have no blocks")
        self.assertTrue(len(mon["blocks"]) > 0, "Monday should have a block")

    def test_planning_without_exclude_weekends_shows_on_saturday(self):
        create_plannings(
            company=self.company,
            project_file=self.project_file,
            planning_type=self.planning_type,
            status=ResourcePlanningStatus.PLANNED,
            notes="",
            resources=[(self.employee_ct, self.employee.pk)],
            date_from=datetime.date(2026, 7, 6),
            date_to=datetime.date(2026, 7, 12),
            daily_start=datetime.time(8, 0),
            daily_end=datetime.time(17, 0),
            exclude_weekends=False,
            is_group=False,
        )
        r = self.http.get(
            self.schedule_url + "?day=2026-07-06&num_days=7"
            + f"&company={self.company.pk}"
        )
        resource_groups = r.context["resource_groups"]
        cells = resource_groups[0]["resources"][0]["cells"]
        sat = next(c for c in cells if c["date_key"] == "2026-07-11")
        self.assertTrue(len(sat["blocks"]) > 0, "Saturday should have a block")


class TestScheduleFilters(_ViewBase):
    @property
    def schedule_url(self):
        return reverse("admin:management_resourceplanning_schedule")

    def test_only_with_plans_shows_only_rows_with_blocks(self):
        create_plannings(
            company=self.company,
            project_file=self.project_file,
            planning_type=self.planning_type,
            status=ResourcePlanningStatus.PLANNED,
            notes="",
            resources=[(self.employee_ct, self.employee.pk)],
            date_from=datetime.date(2026, 7, 6),
            date_to=datetime.date(2026, 7, 6),
            daily_start=datetime.time(8, 0),
            daily_end=datetime.time(17, 0),
            is_group=False,
        )

        response = self.http.get(
            self.schedule_url,
            {
                "day": "2026-07-06",
                "num_days": "7",
                "company": [str(self.company.pk)],
                "only_with_plans": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        rows = [
            resource
            for group in response.context["resource_groups"]
            for resource in group["resources"]
        ]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], str(self.employee))
        self.assertFalse(response.context["show_cell_create_buttons"])

    def test_status_filter_accepts_multiple_values(self):
        create_plannings(
            company=self.company,
            project_file=self.project_file,
            planning_type=self.planning_type,
            status=ResourcePlanningStatus.PLANNED,
            notes="planned",
            resources=[(self.employee_ct, self.employee.pk)],
            date_from=datetime.date(2026, 7, 6),
            date_to=datetime.date(2026, 7, 6),
            daily_start=datetime.time(8, 0),
            daily_end=datetime.time(12, 0),
            is_group=False,
        )
        create_plannings(
            company=self.company,
            project_file=self.project_file,
            planning_type=self.planning_type,
            status=ResourcePlanningStatus.CONFIRMED,
            notes="confirmed",
            resources=[(self.employee_ct, self.employee.pk)],
            date_from=datetime.date(2026, 7, 6),
            date_to=datetime.date(2026, 7, 6),
            daily_start=datetime.time(13, 0),
            daily_end=datetime.time(17, 0),
            is_group=False,
        )

        response = self.http.get(
            self.schedule_url,
            {
                "day": "2026-07-06",
                "num_days": "1",
                "company": [str(self.company.pk)],
                "status": [
                    ResourcePlanningStatus.PLANNED,
                    ResourcePlanningStatus.CONFIRMED,
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        rows = [
            resource
            for group in response.context["resource_groups"]
            for resource in group["resources"]
        ]
        employee_row = next(r for r in rows if r["name"] == str(self.employee))
        blocks = employee_row["cells"][0]["blocks"]
        self.assertEqual(len(blocks), 2)

    def test_resources_filter_works_when_form_is_invalid(self):
        """Resource filter must apply even when other fields invalidate the form."""
        create_plannings(
            company=self.company,
            project_file=self.project_file,
            planning_type=self.planning_type,
            status=ResourcePlanningStatus.PLANNED,
            notes="",
            resources=[(self.employee_ct, self.employee.pk)],
            date_from=datetime.date(2026, 7, 6),
            date_to=datetime.date(2026, 7, 6),
            daily_start=datetime.time(8, 0),
            daily_end=datetime.time(17, 0),
            is_group=False,
        )

        response = self.http.get(
            self.schedule_url,
            {
                "day": "2026-07-06",
                "num_days": "7",
                "company": [str(self.company.pk)],
                "resources": f"employee:{self.employee.pk}",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["filter_form"].is_valid())
        rows = [
            resource
            for group in response.context["resource_groups"]
            for resource in group["resources"]
        ]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], str(self.employee))


# ── Repetition series ─────────────────────────────────────────────────────────

class TestRepetitionSeries(_ViewBase):
    def _make_repetition(self):
        plannings = create_plannings(
            company=self.company,
            project_file=self.project_file,
            planning_type=self.planning_type,
            status=ResourcePlanningStatus.PLANNED,
            notes="",
            resources=[(self.employee_ct, self.employee.pk)],
            date_from=datetime.date(2026, 7, 6),
            date_to=datetime.date(2026, 7, 6),
            daily_start=datetime.time(8, 0),
            daily_end=datetime.time(17, 0),
            repeat="weekly",
            repeat_until=datetime.date(2026, 7, 20),
        )
        group = plannings[0].group
        self.assertTrue(group.is_repetition)
        return group, plannings

    def test_change_view_redirects_repetition_member_to_individual(self):
        group, plannings = self._make_repetition()
        change_url = reverse("admin:management_resourceplanning_change", args=[plannings[0].pk])
        individual_url = reverse(
            "admin:management_resourceplanning_individual_change", args=[plannings[0].pk]
        )
        response = self.http.get(change_url)
        self.assertRedirects(response, individual_url, fetch_redirect_response=False)

    def test_non_repetition_group_still_redirects_to_group_change(self):
        plannings = self._make_group()
        group = plannings[0].group
        planning = plannings[0]
        response = self.http.get(
            reverse("admin:management_resourceplanning_change", args=[planning.pk])
        )
        self.assertRedirects(
            response,
            self.group_url(group.pk),
            fetch_redirect_response=False,
        )

    def test_series_editor_lists_occurrences_with_edit_links(self):
        group, plannings = self._make_repetition()
        response = self.http.get(self.group_url(group.pk))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["plannings"]), len(plannings))
        self.assertContains(response, reverse(
            "admin:management_resourceplanning_individual_change",
            args=[plannings[0].pk],
        ))

    def test_delete_series_removes_all_plannings(self):
        group, plannings = self._make_repetition()
        count = len(plannings)
        self.assertGreater(count, 1)

        response = self.http.post(self.group_url(group.pk), {"delete_group": "1"})
        self.assertRedirects(
            response,
            reverse("admin:management_resourceplanning_changelist"),
            fetch_redirect_response=False,
        )
        self.assertEqual(ResourcePlanning.objects.count(), 0)
        self.assertFalse(ResourcePlanningGroup.objects.filter(pk=group.pk).exists())

    def test_cancel_series_marks_all_as_cancelled(self):
        group, plannings = self._make_repetition()
        response = self.http.post(self.group_url(group.pk), {"cancel_group": "1"})
        self.assertRedirects(
            response,
            reverse("admin:management_resourceplanning_changelist"),
            fetch_redirect_response=False,
        )
        statuses = set(ResourcePlanning.objects.values_list("status", flat=True))
        self.assertEqual(statuses, {ResourcePlanningStatus.CANCELLED})
        self.assertTrue(ResourcePlanningGroup.objects.filter(pk=group.pk).exists())

    def test_individual_edit_detaches_from_repetition_series(self):
        group, plannings = self._make_repetition()
        planning = plannings[0]
        response = self.http.post(
            self.individual_url(planning.pk),
            {
                "company": self.company.pk,
                "project_file": self.project_file.pk,
                "planning_type": self.planning_type.pk,
                "status": ResourcePlanningStatus.CONFIRMED,
                "resources": f"employee:{self.employee.pk}",
                "date_from": "2026-07-06",
                "date_to": "2026-07-06",
                "daily_start": "09:00:00",
                "daily_end": "18:00:00",
                "_save": "1",
            },
        )
        self.assertEqual(response.status_code, 302)

        planning.refresh_from_db()
        self.assertIsNone(planning.group_id)
        self.assertEqual(planning.status, ResourcePlanningStatus.CONFIRMED)
        self.assertEqual(timezone.localtime(planning.start).time(), datetime.time(9, 0))
        remaining = ResourcePlanning.objects.filter(group_id=group.pk).count()
        self.assertEqual(remaining, len(plannings) - 1)


# ── Schedule: delete day from grouped planning ────────────────────────────────

class TestScheduleDeleteDayFromGroup(_ViewBase):
    def setUp(self):
        super().setUp()
        self.plannings = create_plannings(
            company=self.company,
            project_file=self.project_file,
            planning_type=self.planning_type,
            status=ResourcePlanningStatus.PLANNED,
            notes="",
            resources=[
                (self.employee_ct, self.employee.pk),
                (self.employee_ct, self.employee2.pk),
            ],
            date_from=datetime.date(2026, 7, 6),
            date_to=datetime.date(2026, 7, 10),
            daily_start=datetime.time(8, 0),
            daily_end=datetime.time(17, 0),
            is_group=True,
        )
        self.group = self.plannings[0].group
        self.p1, self.p2 = self.plannings[0], self.plannings[1]

    def test_delete_day_detaches_target_keeps_group_for_others(self):
        url = reverse("admin:management_resourceplanning_delete_day", args=[self.p1.pk])
        r = self.http.post(url, {
            "day": "2026-07-08",
            "next": reverse("admin:management_resourceplanning_schedule"),
        })
        self.assertEqual(r.status_code, 302)

        self.p1.refresh_from_db()
        self.p2.refresh_from_db()
        self.assertIsNone(self.p1.group_id)
        self.assertEqual(self.p2.group_id, self.group.pk)
        self.assertTrue(ResourcePlanningGroup.objects.filter(pk=self.group.pk).exists())


# ── Multi add view ────────────────────────────────────────────────────────────

class TestMultiAddView(_ViewBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.project_file2 = ProjectFile.objects.create(
            company=cls.company, reference="EXP-00002", name="Second project",
            status="open", client=cls.client_obj,
        )
        cls.planning_type2 = ResourcePlanningType.objects.create(
            company=cls.company, name="Transport", color="#EF4444",
        )

    @property
    def multi_add_url(self):
        return reverse("admin:management_resourceplanning_multi_add")

    def _multi_post_data(self, **overrides):
        data = {
            "company": self.company.pk,
            "status": ResourcePlanningStatus.PLANNED,
            "resources": f"employee:{self.employee.pk}",
            "lines-TOTAL_FORMS": "2",
            "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "1",
            "lines-MAX_NUM_FORMS": "1000",
            "lines-0-project_file": self.project_file.pk,
            "lines-0-planning_type": self.planning_type.pk,
            "lines-0-date_from": "2026-07-07",
            "lines-0-date_to": "2026-07-08",
            "lines-0-daily_start": "08:00:00",
            "lines-0-daily_end": "17:00:00",
            "lines-1-project_file": self.project_file2.pk,
            "lines-1-planning_type": self.planning_type2.pk,
            "lines-1-date_from": "2026-07-09",
            "lines-1-date_to": "2026-07-10",
            "lines-1-daily_start": "08:00:00",
            "lines-1-daily_end": "17:00:00",
            "_save": "1",
        }
        data.update(overrides)
        return data

    def test_get_renders_form(self):
        r = self.http.get(self.multi_add_url)
        self.assertEqual(r.status_code, 200)

    def test_get_prepopulates_resources(self):
        r = self.http.get(self.multi_add_url + f"?resources=employee:{self.employee.pk}")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, f"employee:{self.employee.pk}")

    def test_post_creates_one_planning_per_line(self):
        r = self.http.post(self.multi_add_url, self._multi_post_data())
        self.assertEqual(r.status_code, 302)
        self.assertEqual(ResourcePlanning.objects.count(), 2)
        self.assertEqual(ResourcePlanningGroup.objects.count(), 0)

    def test_post_each_line_keeps_own_fields(self):
        self.http.post(self.multi_add_url, self._multi_post_data())
        first = ResourcePlanning.objects.get(project_file=self.project_file)
        second = ResourcePlanning.objects.get(project_file=self.project_file2)
        self.assertEqual(first.planning_type, self.planning_type)
        self.assertEqual(second.planning_type, self.planning_type2)

    def test_post_shared_status_applied(self):
        self.http.post(self.multi_add_url, self._multi_post_data(
            status=ResourcePlanningStatus.CONFIRMED,
        ))
        for p in ResourcePlanning.objects.all():
            self.assertEqual(p.status, ResourcePlanningStatus.CONFIRMED)

    def test_post_per_line_options_applied(self):
        self.http.post(self.multi_add_url, self._multi_post_data(**{
            "lines-0-notes": "first",
            "lines-0-show_planning_type": "on",
            "lines-0-exclude_weekends": "on",
            "lines-0-daily_start": "06:00:00",
            "lines-0-daily_end": "14:00:00",
            "lines-1-notes": "second",
        }))
        first = ResourcePlanning.objects.get(project_file=self.project_file)
        second = ResourcePlanning.objects.get(project_file=self.project_file2)
        self.assertEqual(first.notes, "first")
        self.assertTrue(first.show_planning_type)
        self.assertTrue(first.exclude_weekends)
        self.assertEqual(second.notes, "second")
        self.assertFalse(second.show_planning_type)
        self.assertFalse(second.exclude_weekends)

    def test_post_save_redirects_to_changelist(self):
        r = self.http.post(self.multi_add_url, self._multi_post_data())
        self.assertRedirects(
            r, reverse("admin:management_resourceplanning_changelist"),
            fetch_redirect_response=False,
        )

    def test_post_addanother_redirects_to_multi_add(self):
        data = self._multi_post_data()
        del data["_save"]
        data["_addanother"] = "1"
        r = self.http.post(self.multi_add_url, data)
        self.assertRedirects(r, self.multi_add_url, fetch_redirect_response=False)

    def test_post_overlapping_lines_shows_error_creates_nothing(self):
        data = self._multi_post_data(**{"lines-1-date_from": "2026-07-08"})
        r = self.http.post(self.multi_add_url, data)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context["errors"])
        self.assertEqual(ResourcePlanning.objects.count(), 0)

    def test_post_deleted_line_is_skipped(self):
        data = self._multi_post_data(**{"lines-1-DELETE": "on"})
        r = self.http.post(self.multi_add_url, data)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(ResourcePlanning.objects.count(), 1)
        self.assertEqual(
            ResourcePlanning.objects.get().project_file, self.project_file
        )

    def test_post_missing_line_fields_rerenders(self):
        data = self._multi_post_data(**{"lines-0-project_file": ""})
        r = self.http.post(self.multi_add_url, data)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(ResourcePlanning.objects.count(), 0)


# ── Insert-between (shift_following) via add view ─────────────────────────────

class TestAddViewShiftFollowing(_ViewBase):
    def setUp(self):
        super().setUp()
        day = datetime.date(2026, 7, 7)
        self.morning = self._make_individual(date_from=day, date_to=day)
        self.morning.start = self.morning.start.replace(hour=8, minute=0)
        self.morning.end = self.morning.end.replace(hour=10, minute=0)
        self.morning.save()
        self.midday = create_plannings(
            company=self.company,
            project_file=self.project_file,
            planning_type=self.planning_type,
            status=ResourcePlanningStatus.PLANNED,
            notes="",
            resources=[(self.employee_ct, self.employee.pk)],
            date_from=day,
            date_to=day,
            daily_start=datetime.time(10, 0),
            daily_end=datetime.time(12, 0),
            exclude_weekends=False,
            is_group=False,
        )[0]

    def test_get_prepopulates_shift_following(self):
        r = self.http.get(self.add_url + "?shift_following=1&day=2026-07-07")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context["form"].initial.get("shift_following"))

    def test_post_with_shift_inserts_and_pushes_later_plan(self):
        data = self._post_data(
            date_from="2026-07-07",
            date_to="2026-07-07",
            daily_start="10:00:00",
            daily_end="11:00:00",
            shift_following="1",
        )
        r = self.http.post(self.add_url, data)
        self.assertEqual(r.status_code, 302)
        self.midday.refresh_from_db()
        from django.utils import timezone as _tz
        self.assertEqual(_tz.localtime(self.midday.start).time(), datetime.time(11, 0))
        self.assertEqual(_tz.localtime(self.midday.end).time(), datetime.time(13, 0))
        self.assertEqual(ResourcePlanning.objects.count(), 3)

    def test_post_shift_crossing_midnight_shows_error(self):
        # Contiguous with the 10–12 plan so the domino chain reaches it
        evening = create_plannings(
            company=self.company,
            project_file=self.project_file,
            planning_type=self.planning_type,
            status=ResourcePlanningStatus.PLANNED,
            notes="",
            resources=[(self.employee_ct, self.employee.pk)],
            date_from=datetime.date(2026, 7, 7),
            date_to=datetime.date(2026, 7, 7),
            daily_start=datetime.time(12, 0),
            daily_end=datetime.time(23, 30),
            exclude_weekends=False,
            is_group=False,
        )[0]
        data = self._post_data(
            date_from="2026-07-07",
            date_to="2026-07-07",
            daily_start="10:00:00",
            daily_end="11:00:00",
            shift_following="1",
        )
        r = self.http.post(self.add_url, data)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context["errors"])
        evening.refresh_from_db()
        from django.utils import timezone as _tz
        self.assertEqual(_tz.localtime(evening.start).time(), datetime.time(12, 0))
        self.assertEqual(ResourcePlanning.objects.count(), 3)  # nothing new saved
