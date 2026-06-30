"""
Service-layer tests for resource planning.

Covers:
- iter_working_days
- create_plannings (individual / grouped / exclude_weekends)
- update_group (replaces plannings, overlap detection)
- update_planning (field update, exclude_weekends persisted)
- dissolve_group
- ResourcePlanning.cancel / ResourcePlanningGroup.cancel_all
"""
import datetime

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from company.models import Company, Employee
from sales.models import Client
from management.models import (
    ProjectFile,
    ResourcePlanning,
    ResourcePlanningGroup,
    ResourcePlanningStatus,
    ResourcePlanningType,
)
from management.services.resource_planning import (
    create_plannings,
    delete_day_from_planning,
    dissolve_group,
    iter_working_days,
    update_group,
    update_planning,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Test Co")

        cls.employee_ct = ContentType.objects.get(app_label="company", model="employee")
        _hired = datetime.date(2020, 1, 1)
        cls.employee = Employee.objects.create(
            company=cls.company,
            first_name="Ana",
            last_name="García",
            hired_at=_hired,
            is_active=True,
        )
        cls.employee2 = Employee.objects.create(
            company=cls.company,
            first_name="Luis",
            last_name="Pérez",
            hired_at=_hired,
            is_active=True,
        )

        cls.client = Client.objects.create(company=cls.company, name="Test Client")
        cls.project_file = ProjectFile.objects.create(
            company=cls.company,
            reference="EXP-00001",
            name="Test project",
            status="open",
            client=cls.client,
        )
        cls.planning_type = ResourcePlanningType.objects.create(
            company=cls.company,
            name="Installation",
            color="#3B82F6",
        )

        cls.resources = [
            (cls.employee_ct, cls.employee.pk),
        ]
        cls.two_resources = [
            (cls.employee_ct, cls.employee.pk),
            (cls.employee_ct, cls.employee2.pk),
        ]

        cls.date_from = datetime.date(2026, 7, 7)   # Tuesday
        cls.date_to   = datetime.date(2026, 7, 9)   # Thursday
        cls.t_start   = datetime.time(8, 0)
        cls.t_end     = datetime.time(17, 0)

    def _make_planning(self, *, resource=None, date_from=None, date_to=None,
                       t_start=None, t_end=None, group=None, exclude_weekends=False):
        resource = resource or (self.employee_ct, self.employee.pk)
        ct, obj_id = resource
        start = timezone.make_aware(datetime.datetime.combine(
            date_from or self.date_from, t_start or self.t_start
        ))
        end = timezone.make_aware(datetime.datetime.combine(
            date_to or self.date_to, t_end or self.t_end
        ))
        return ResourcePlanning.objects.create(
            company=self.company,
            project_file=self.project_file,
            planning_type=self.planning_type,
            status=ResourcePlanningStatus.PLANNED,
            resource_content_type=ct,
            resource_object_id=obj_id,
            start=start,
            end=end,
            exclude_weekends=exclude_weekends,
            group=group,
        )


# ── iter_working_days ─────────────────────────────────────────────────────────

class TestIterWorkingDays(TestCase):
    def test_single_day(self):
        days = iter_working_days(datetime.date(2026, 7, 7), datetime.date(2026, 7, 7), True)
        self.assertEqual(days, [datetime.date(2026, 7, 7)])

    def test_full_week_no_exclusion(self):
        # Mon–Sun, exclude_weekends=False → 7 days
        days = iter_working_days(datetime.date(2026, 7, 6), datetime.date(2026, 7, 12), False)
        self.assertEqual(len(days), 7)

    def test_full_week_with_exclusion(self):
        # Mon–Sun, exclude_weekends=True → 5 days (Mon–Fri)
        days = iter_working_days(datetime.date(2026, 7, 6), datetime.date(2026, 7, 12), True)
        self.assertEqual(len(days), 5)
        self.assertTrue(all(d.weekday() < 5 for d in days))

    def test_weekend_only_range_excluded(self):
        # Sat–Sun with exclude_weekends=True → empty
        days = iter_working_days(datetime.date(2026, 7, 11), datetime.date(2026, 7, 12), True)
        self.assertEqual(days, [])

    def test_weekend_only_range_included(self):
        days = iter_working_days(datetime.date(2026, 7, 11), datetime.date(2026, 7, 12), False)
        self.assertEqual(len(days), 2)


# ── create_plannings ──────────────────────────────────────────────────────────

class TestCreatePlannings(_Base):
    def _call(self, **overrides):
        kwargs = dict(
            company=self.company,
            project_file=self.project_file,
            planning_type=self.planning_type,
            status=ResourcePlanningStatus.PLANNED,
            notes="",
            resources=self.resources,
            date_from=self.date_from,
            date_to=self.date_to,
            daily_start=self.t_start,
            daily_end=self.t_end,
            exclude_weekends=False,
            is_group=False,
        )
        kwargs.update(overrides)
        return create_plannings(**kwargs)

    def test_creates_one_record_per_resource(self):
        plannings = self._call()
        self.assertEqual(len(plannings), 1)
        self.assertEqual(ResourcePlanning.objects.count(), 1)

    def test_two_resources_creates_two_records(self):
        plannings = self._call(resources=self.two_resources)
        self.assertEqual(len(plannings), 2)
        self.assertEqual(ResourcePlanning.objects.count(), 2)

    def test_start_end_datetimes_are_correct(self):
        plannings = self._call()
        p = ResourcePlanning.objects.get()
        local_start = timezone.localtime(p.start)
        local_end   = timezone.localtime(p.end)
        self.assertEqual(local_start.date(), self.date_from)
        self.assertEqual(local_start.time(), self.t_start)
        self.assertEqual(local_end.date(), self.date_to)
        self.assertEqual(local_end.time(), self.t_end)

    def test_exclude_weekends_saved_on_model(self):
        self._call(exclude_weekends=True)
        self.assertTrue(ResourcePlanning.objects.get().exclude_weekends)

    def test_is_group_false_creates_no_group(self):
        plannings = self._call(is_group=False)
        self.assertIsNone(plannings[0].group_id)
        self.assertEqual(ResourcePlanningGroup.objects.count(), 0)

    def test_is_group_true_links_plannings_to_group(self):
        plannings = self._call(resources=self.two_resources, is_group=True)
        group_ids = {p.group_id for p in plannings}
        self.assertEqual(len(group_ids), 1)
        self.assertIsNotNone(list(group_ids)[0])
        self.assertEqual(ResourcePlanningGroup.objects.count(), 1)

    def test_overlap_raises_validation_error(self):
        self._call()  # create first planning
        with self.assertRaises(ValidationError):
            self._call()  # overlapping second planning for same resource

    def test_no_working_days_raises(self):
        # Sat–Sun with exclude_weekends=True → no working days
        with self.assertRaises(ValidationError):
            self._call(
                date_from=datetime.date(2026, 7, 11),
                date_to=datetime.date(2026, 7, 12),
                exclude_weekends=True,
            )

    def test_end_before_start_raises(self):
        with self.assertRaises(ValidationError):
            self._call(
                date_from=datetime.date(2026, 7, 7),
                date_to=datetime.date(2026, 7, 7),
                daily_start=datetime.time(17, 0),
                daily_end=datetime.time(8, 0),
            )

    def test_multi_day_allows_end_time_before_start_time_when_span_valid(self):
        # Jul 7 17:00 → Jul 9 06:00: clock times look inverted but span is valid.
        plannings = self._call(
            daily_start=datetime.time(17, 0),
            daily_end=datetime.time(6, 0),
        )
        self.assertEqual(len(plannings), 1)
        local_start = timezone.localtime(plannings[0].start)
        local_end = timezone.localtime(plannings[0].end)
        self.assertEqual(local_start.date(), self.date_from)
        self.assertEqual(local_end.date(), self.date_to)


# ── update_group ──────────────────────────────────────────────────────────────

class TestUpdateGroup(_Base):
    def setUp(self):
        self.plannings = create_plannings(
            company=self.company,
            project_file=self.project_file,
            planning_type=self.planning_type,
            status=ResourcePlanningStatus.PLANNED,
            notes="",
            resources=self.two_resources,
            date_from=self.date_from,
            date_to=self.date_to,
            daily_start=self.t_start,
            daily_end=self.t_end,
            exclude_weekends=False,
            is_group=True,
        )
        self.group = self.plannings[0].group

    def test_replaces_old_plannings(self):
        old_pks = {p.pk for p in self.plannings}
        update_group(
            self.group.pk,
            company=self.company,
            project_file=self.project_file,
            planning_type=self.planning_type,
            status=ResourcePlanningStatus.CONFIRMED,
            notes="updated",
            resources=self.resources,  # one resource instead of two
            date_from=self.date_from,
            date_to=self.date_to,
            daily_start=self.t_start,
            daily_end=self.t_end,
            exclude_weekends=False,
        )
        new_pks = set(ResourcePlanning.objects.values_list("pk", flat=True))
        self.assertFalse(old_pks & new_pks, "Old plannings should have been deleted")
        self.assertEqual(ResourcePlanning.objects.count(), 1)

    def test_updated_status_saved(self):
        update_group(
            self.group.pk,
            company=self.company,
            project_file=self.project_file,
            planning_type=self.planning_type,
            status=ResourcePlanningStatus.CONFIRMED,
            notes="",
            resources=self.resources,
            date_from=self.date_from,
            date_to=self.date_to,
            daily_start=self.t_start,
            daily_end=self.t_end,
            exclude_weekends=False,
        )
        self.assertEqual(ResourcePlanning.objects.get().status, ResourcePlanningStatus.CONFIRMED)

    def test_overlap_with_external_planning_raises(self):
        # Create a conflicting planning for employee that is NOT in the group
        employee3 = Employee.objects.create(
            company=self.company, first_name="X", last_name="Y",
            hired_at=datetime.date(2020, 1, 1), is_active=True,
        )
        self._make_planning(resource=(self.employee_ct, employee3.pk))

        with self.assertRaises(ValidationError):
            update_group(
                self.group.pk,
                company=self.company,
                project_file=self.project_file,
                planning_type=self.planning_type,
                status=ResourcePlanningStatus.PLANNED,
                notes="",
                resources=[(self.employee_ct, employee3.pk)],
                date_from=self.date_from,
                date_to=self.date_to,
                daily_start=self.t_start,
                daily_end=self.t_end,
                exclude_weekends=False,
            )


# ── update_planning ───────────────────────────────────────────────────────────

class TestUpdatePlanning(_Base):
    def setUp(self):
        self.planning = self._make_planning()

    def test_updates_notes(self):
        update_planning(self.planning.pk, notes="hello")
        self.assertEqual(ResourcePlanning.objects.get(pk=self.planning.pk).notes, "hello")

    def test_updates_status(self):
        update_planning(self.planning.pk, status=ResourcePlanningStatus.CONFIRMED)
        self.assertEqual(
            ResourcePlanning.objects.get(pk=self.planning.pk).status,
            ResourcePlanningStatus.CONFIRMED,
        )

    def test_updates_exclude_weekends(self):
        update_planning(self.planning.pk, exclude_weekends=True)
        self.assertTrue(ResourcePlanning.objects.get(pk=self.planning.pk).exclude_weekends)

    def test_end_before_start_raises(self):
        bad_end = timezone.make_aware(
            datetime.datetime.combine(self.date_from, datetime.time(7, 0))
        )
        with self.assertRaises((ValidationError, Exception)):
            update_planning(self.planning.pk, end=bad_end)


# ── dissolve_group ────────────────────────────────────────────────────────────

class TestDissolveGroup(_Base):
    def setUp(self):
        plannings = create_plannings(
            company=self.company,
            project_file=self.project_file,
            planning_type=self.planning_type,
            status=ResourcePlanningStatus.PLANNED,
            notes="",
            resources=self.two_resources,
            date_from=self.date_from,
            date_to=self.date_to,
            daily_start=self.t_start,
            daily_end=self.t_end,
            exclude_weekends=False,
            is_group=True,
        )
        self.group = plannings[0].group

    def test_group_deleted(self):
        dissolve_group(self.group.pk)
        self.assertFalse(ResourcePlanningGroup.objects.filter(pk=self.group.pk).exists())

    def test_plannings_become_individual(self):
        dissolve_group(self.group.pk)
        self.assertEqual(
            ResourcePlanning.objects.filter(group__isnull=False).count(), 0
        )
        self.assertEqual(ResourcePlanning.objects.count(), 2)

    def test_dissolved_plannings_still_editable(self):
        dissolve_group(self.group.pk)
        p = ResourcePlanning.objects.first()
        update_planning(p.pk, notes="now individual")
        self.assertEqual(ResourcePlanning.objects.get(pk=p.pk).notes, "now individual")


# ── ResourcePlanningGroup.cancel_all / ResourcePlanning.cancel ────────────────

class TestCancelActions(_Base):
    def setUp(self):
        plannings = create_plannings(
            company=self.company,
            project_file=self.project_file,
            planning_type=self.planning_type,
            status=ResourcePlanningStatus.PLANNED,
            notes="",
            resources=self.two_resources,
            date_from=self.date_from,
            date_to=self.date_to,
            daily_start=self.t_start,
            daily_end=self.t_end,
            exclude_weekends=False,
            is_group=True,
        )
        self.group = plannings[0].group
        self.p1, self.p2 = plannings[0], plannings[1]

    def test_cancel_all_cancels_every_planning_in_group(self):
        self.group.cancel_all()
        statuses = set(ResourcePlanning.objects.values_list("status", flat=True))
        self.assertEqual(statuses, {ResourcePlanningStatus.CANCELLED})

    def test_cancel_all_leaves_group_intact(self):
        self.group.cancel_all()
        self.assertTrue(ResourcePlanningGroup.objects.filter(pk=self.group.pk).exists())

    def test_cancel_only_affects_one_planning(self):
        self.p1.cancel()
        self.assertEqual(
            ResourcePlanning.objects.get(pk=self.p1.pk).status,
            ResourcePlanningStatus.CANCELLED,
        )
        self.assertEqual(
            ResourcePlanning.objects.get(pk=self.p2.pk).status,
            ResourcePlanningStatus.PLANNED,
        )


# ── delete_day_from_planning ──────────────────────────────────────────────────

class TestDeleteDayFromPlanning(_Base):
    def setUp(self):
        self.plannings = create_plannings(
            company=self.company,
            project_file=self.project_file,
            planning_type=self.planning_type,
            status=ResourcePlanningStatus.PLANNED,
            notes="",
            resources=self.two_resources,
            date_from=datetime.date(2026, 7, 6),   # Mon
            date_to=datetime.date(2026, 7, 10),    # Fri
            daily_start=self.t_start,
            daily_end=self.t_end,
            exclude_weekends=False,
            is_group=True,
        )
        self.group = self.plannings[0].group
        self.p1, self.p2 = self.plannings[0], self.plannings[1]

    def test_detach_target_from_group_others_remain_grouped(self):
        delete_day_from_planning(self.p1.pk, datetime.date(2026, 7, 8))  # Wed

        self.p1.refresh_from_db()
        self.p2.refresh_from_db()
        self.assertIsNone(self.p1.group_id)
        self.assertEqual(self.p2.group_id, self.group.pk)
        self.assertTrue(ResourcePlanningGroup.objects.filter(pk=self.group.pk).exists())

    def test_other_group_member_dates_unchanged(self):
        p2_start_before = self.p2.start
        p2_end_before = self.p2.end

        delete_day_from_planning(self.p1.pk, datetime.date(2026, 7, 8))

        self.p2.refresh_from_db()
        self.assertEqual(self.p2.start, p2_start_before)
        self.assertEqual(self.p2.end, p2_end_before)

    def test_delete_first_day_removes_only_target_from_group(self):
        delete_day_from_planning(self.p1.pk, datetime.date(2026, 7, 6))

        self.assertTrue(ResourcePlanning.objects.filter(pk=self.p1.pk).exists())
        self.p1.refresh_from_db()
        self.assertIsNone(self.p1.group_id)
        local_start = timezone.localtime(self.p1.start)
        self.assertEqual(local_start.date(), datetime.date(2026, 7, 7))

        self.p2.refresh_from_db()
        self.assertEqual(self.p2.group_id, self.group.pk)
        p2_start = timezone.localtime(self.p2.start)
        self.assertEqual(p2_start.date(), datetime.date(2026, 7, 6))

    def test_split_day_creates_ungrouped_fragments(self):
        delete_day_from_planning(self.p1.pk, datetime.date(2026, 7, 8))

        p1_plannings = ResourcePlanning.objects.filter(
            resource_object_id=self.p1.resource_object_id,
        )
        self.assertEqual(p1_plannings.count(), 2)
        self.assertTrue(all(p.group_id is None for p in p1_plannings))

    def test_individual_planning_no_group_side_effects(self):
        solo = create_plannings(
            company=self.company,
            project_file=self.project_file,
            planning_type=self.planning_type,
            status=ResourcePlanningStatus.PLANNED,
            notes="",
            resources=[(self.employee_ct, self.employee2.pk)],
            date_from=datetime.date(2026, 8, 7),
            date_to=datetime.date(2026, 8, 9),
            daily_start=self.t_start,
            daily_end=self.t_end,
            is_group=False,
        )[0]

        delete_day_from_planning(solo.pk, datetime.date(2026, 8, 9))

        solo.refresh_from_db()
        self.assertIsNone(solo.group_id)
        local_end = timezone.localtime(solo.end)
        self.assertEqual(local_end.date(), datetime.date(2026, 8, 8))