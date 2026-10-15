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

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from company.models import Employee
from management.models import (
    ProjectFile,
    ResourcePlanning,
    ResourcePlanningGroup,
    ResourcePlanningStatus,
    ResourcePlanningType,
)
from management.services.resource_planning import (
    create_multi_plannings,
    delete_day_from_planning,
    dissolve_group,
    iter_working_days,
    update_group,
    update_planning,
)
from management.tests.factories import ResourcePlanningFixturesMixin


# ── Fixtures ──────────────────────────────────────────────────────────────────

class _Base(ResourcePlanningFixturesMixin, TestCase):
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
    def test_creates_one_record_per_resource(self):
        plannings = self._create()
        self.assertEqual(len(plannings), 1)
        self.assertEqual(ResourcePlanning.objects.count(), 1)
        self.assertIsNone(plannings[0].group_id)
        self.assertEqual(ResourcePlanningGroup.objects.count(), 0)

    def test_two_resources_creates_two_records(self):
        plannings = self._create(resources=self.two_resources)
        self.assertEqual(len(plannings), 2)
        self.assertEqual(ResourcePlanning.objects.count(), 2)

    def test_start_end_datetimes_are_correct(self):
        plannings = self._create()
        p = ResourcePlanning.objects.get()
        local_start = timezone.localtime(p.start)
        local_end   = timezone.localtime(p.end)
        self.assertEqual(local_start.date(), self.date_from)
        self.assertEqual(local_start.time(), self.t_start)
        self.assertEqual(local_end.date(), self.date_to)
        self.assertEqual(local_end.time(), self.t_end)

    def test_exclude_weekends_saved_on_model(self):
        self._create(exclude_weekends=True)
        self.assertTrue(ResourcePlanning.objects.get().exclude_weekends)

    def test_is_group_true_links_plannings_to_group(self):
        plannings = self._create(resources=self.two_resources, is_group=True)
        group_ids = {p.group_id for p in plannings}
        self.assertEqual(len(group_ids), 1)
        self.assertIsNotNone(list(group_ids)[0])
        self.assertEqual(ResourcePlanningGroup.objects.count(), 1)

    def test_overlap_raises_validation_error(self):
        self._create()  # create first planning
        with self.assertRaises(ValidationError):
            self._create()  # overlapping second planning for same resource

    def test_no_working_days_raises(self):
        # Sat–Sun with exclude_weekends=True → no working days
        with self.assertRaises(ValidationError):
            self._create(
                date_from=datetime.date(2026, 7, 11),
                date_to=datetime.date(2026, 7, 12),
                exclude_weekends=True,
            )

    def test_end_before_start_raises(self):
        with self.assertRaises(ValidationError):
            self._create(
                date_from=datetime.date(2026, 7, 7),
                date_to=datetime.date(2026, 7, 7),
                daily_start=datetime.time(17, 0),
                daily_end=datetime.time(8, 0),
            )

    def test_multi_day_allows_end_time_before_start_time_when_span_valid(self):
        # Jul 7 17:00 → Jul 9 06:00: clock times look inverted but span is valid.
        plannings = self._create(
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
        self.plannings = self._create(resources=self.two_resources, is_group=True)
        self.group = self.plannings[0].group

    def test_replaces_old_plannings_with_updated_fields(self):
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
        with self.assertRaises(ValidationError):
            update_planning(self.planning.pk, end=bad_end)


# ── dissolve_group ────────────────────────────────────────────────────────────

class TestDissolveGroup(_Base):
    def setUp(self):
        plannings = self._create(resources=self.two_resources, is_group=True)
        self.group = plannings[0].group

    def test_group_deleted_and_plannings_become_individual(self):
        dissolve_group(self.group.pk)
        self.assertFalse(ResourcePlanningGroup.objects.filter(pk=self.group.pk).exists())
        self.assertEqual(
            ResourcePlanning.objects.filter(group__isnull=False).count(), 0
        )
        self.assertEqual(ResourcePlanning.objects.count(), 2)


# ── ResourcePlanningGroup.cancel_all / ResourcePlanning.cancel ────────────────

class TestCancelActions(_Base):
    def setUp(self):
        plannings = self._create(resources=self.two_resources, is_group=True)
        self.group = plannings[0].group
        self.p1, self.p2 = plannings[0], plannings[1]

    def test_cancel_all_cancels_every_planning_and_leaves_group_intact(self):
        self.group.cancel_all()
        statuses = set(ResourcePlanning.objects.values_list("status", flat=True))
        self.assertEqual(statuses, {ResourcePlanningStatus.CANCELLED})
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


class TestDeleteGroup(_Base):
    def setUp(self):
        plannings = self._create(resources=self.two_resources, is_group=True)
        self.group = plannings[0].group
        self.plannings = plannings

    def test_delete_group_removes_plannings_and_group(self):
        from management.services.resource_planning import delete_group

        delete_group(self.group.pk)
        self.assertEqual(ResourcePlanning.objects.count(), 0)
        self.assertFalse(ResourcePlanningGroup.objects.filter(pk=self.group.pk).exists())


# ── delete_day_from_planning ──────────────────────────────────────────────────

class TestDeleteDayFromPlanning(_Base):
    def setUp(self):
        self.plannings = self._create(
            resources=self.two_resources,
            date_from=datetime.date(2026, 7, 6),   # Mon
            date_to=datetime.date(2026, 7, 10),    # Fri
            is_group=True,
        )
        self.group = self.plannings[0].group
        self.p1, self.p2 = self.plannings[0], self.plannings[1]

    def test_split_day_detaches_target_into_ungrouped_fragments_others_untouched(self):
        p2_start_before = self.p2.start
        p2_end_before = self.p2.end

        delete_day_from_planning(self.p1.pk, datetime.date(2026, 7, 8))  # Wed

        self.p1.refresh_from_db()
        self.p2.refresh_from_db()
        self.assertIsNone(self.p1.group_id)
        self.assertEqual(self.p2.group_id, self.group.pk)
        self.assertTrue(ResourcePlanningGroup.objects.filter(pk=self.group.pk).exists())
        self.assertEqual(self.p2.start, p2_start_before)
        self.assertEqual(self.p2.end, p2_end_before)

        p1_plannings = ResourcePlanning.objects.filter(
            resource_object_id=self.p1.resource_object_id,
        )
        self.assertEqual(p1_plannings.count(), 2)
        self.assertTrue(all(p.group_id is None for p in p1_plannings))

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

    def test_individual_planning_no_group_side_effects(self):
        solo = self._create(
            resources=[(self.employee_ct, self.employee2.pk)],
            date_from=datetime.date(2026, 8, 7),
            date_to=datetime.date(2026, 8, 9),
            exclude_weekends=True,
        )[0]

        delete_day_from_planning(solo.pk, datetime.date(2026, 8, 9))

        solo.refresh_from_db()
        self.assertIsNone(solo.group_id)
        local_end = timezone.localtime(solo.end)
        self.assertEqual(local_end.date(), datetime.date(2026, 8, 8))

# ── create_multi_plannings ────────────────────────────────────────────────────

class TestCreateMultiPlannings(_Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.project_file2 = ProjectFile.objects.create(
            company=cls.company,
            reference="EXP-00002",
            name="Second project",
            status="open",
            client=cls.client_obj,
        )
        cls.planning_type2 = ResourcePlanningType.objects.create(
            company=cls.company,
            name="Transport",
            color="#EF4444",
        )

    def _line(self, **overrides):
        line = {
            "project_file": self.project_file,
            "planning_type": self.planning_type,
            "date_from": datetime.date(2026, 7, 7),
            "date_to": datetime.date(2026, 7, 8),
            "daily_start": self.t_start,
            "daily_end": self.t_end,
            "exclude_weekends": True,
            "show_planning_type": False,
            "notes": "",
        }
        line.update(overrides)
        return line

    def _lines(self):
        return [
            self._line(),
            self._line(
                project_file=self.project_file2,
                planning_type=self.planning_type2,
                date_from=datetime.date(2026, 7, 9),
                date_to=datetime.date(2026, 7, 10),
            ),
        ]

    def _create(self, *, resources=None, lines=None, **kwargs):
        params = dict(
            company=self.company,
            status=ResourcePlanningStatus.PLANNED,
            resources=self.resources if resources is None else resources,
            lines=self._lines() if lines is None else lines,
        )
        params.update(kwargs)
        return create_multi_plannings(**params)

    def test_creates_one_ungrouped_record_per_line_and_resource(self):
        plannings = self._create(resources=self.two_resources)
        self.assertEqual(len(plannings), 4)
        self.assertEqual(ResourcePlanning.objects.count(), 4)
        self.assertTrue(all(p.group_id is None for p in plannings))
        self.assertEqual(ResourcePlanningGroup.objects.count(), 0)

    def test_each_line_keeps_its_own_fields(self):
        self._create()
        first = ResourcePlanning.objects.get(project_file=self.project_file)
        second = ResourcePlanning.objects.get(project_file=self.project_file2)
        self.assertEqual(first.planning_type, self.planning_type)
        self.assertEqual(second.planning_type, self.planning_type2)
        self.assertEqual(timezone.localtime(first.start).date(), datetime.date(2026, 7, 7))
        self.assertEqual(timezone.localtime(second.end).date(), datetime.date(2026, 7, 10))

    def test_shared_status_applied_to_all(self):
        self._create(status=ResourcePlanningStatus.CONFIRMED)
        for p in ResourcePlanning.objects.all():
            self.assertEqual(p.status, ResourcePlanningStatus.CONFIRMED)

    def test_per_line_options_applied(self):
        lines = [
            self._line(
                notes="first",
                show_planning_type=True,
                exclude_weekends=False,
                daily_start=datetime.time(6, 0),
                daily_end=datetime.time(14, 0),
            ),
            self._line(
                project_file=self.project_file2,
                planning_type=self.planning_type2,
                date_from=datetime.date(2026, 7, 9),
                date_to=datetime.date(2026, 7, 10),
                notes="second",
            ),
        ]
        self._create(lines=lines)
        first = ResourcePlanning.objects.get(project_file=self.project_file)
        second = ResourcePlanning.objects.get(project_file=self.project_file2)
        self.assertEqual(first.notes, "first")
        self.assertTrue(first.show_planning_type)
        self.assertFalse(first.exclude_weekends)
        self.assertEqual(timezone.localtime(first.start).time(), datetime.time(6, 0))
        self.assertEqual(timezone.localtime(first.end).time(), datetime.time(14, 0))
        self.assertEqual(second.notes, "second")
        self.assertFalse(second.show_planning_type)
        self.assertTrue(second.exclude_weekends)

    def test_overlapping_lines_raise(self):
        lines = self._lines()
        lines[1]["date_from"] = datetime.date(2026, 7, 8)  # overlaps line 1's end
        with self.assertRaises(ValidationError):
            self._create(lines=lines)
        self.assertEqual(ResourcePlanning.objects.count(), 0)

    def test_overlap_with_existing_planning_raises_and_creates_nothing(self):
        self._make_planning(
            date_from=datetime.date(2026, 7, 9),
            date_to=datetime.date(2026, 7, 10),
        )
        with self.assertRaises(ValidationError):
            self._create()
        self.assertEqual(ResourcePlanning.objects.count(), 1)  # only the pre-existing one

    def test_empty_lines_raise(self):
        with self.assertRaises(ValidationError):
            self._create(lines=[])

    def test_no_resources_raises(self):
        with self.assertRaises(ValidationError):
            self._create(resources=[])

    def test_line_end_before_start_raises(self):
        lines = self._lines()
        lines[0]["date_to"] = datetime.date(2026, 7, 6)
        with self.assertRaises(ValidationError):
            self._create(lines=lines)


# ── create_plannings with shift_following ─────────────────────────────────────

class TestShiftFollowingPlannings(_Base):
    """Inserting a planning between two existing ones pushes the later ones
    forward by the new planning's duration (same resource, same day only)."""

    def _insert(self, t_start, t_end, *, shift=True, resources=None):
        return self._create(
            resources=resources or self.resources,
            date_to=self.date_from,
            daily_start=t_start,
            daily_end=t_end,
            shift_following=shift,
        )

    def setUp(self):
        # Two plans for the employee on 2026-07-07: 08–10 and 10–12
        self.morning = self._make_planning(
            date_from=self.date_from, date_to=self.date_from,
            t_start=datetime.time(8, 0), t_end=datetime.time(10, 0),
        )
        self.midday = self._make_planning(
            date_from=self.date_from, date_to=self.date_from,
            t_start=datetime.time(10, 0), t_end=datetime.time(12, 0),
        )

    def test_insert_shifts_later_plan_by_duration(self):
        self._insert(datetime.time(10, 0), datetime.time(11, 0))

        self.midday.refresh_from_db()
        self.morning.refresh_from_db()
        self.assertEqual(timezone.localtime(self.midday.start).time(), datetime.time(11, 0))
        self.assertEqual(timezone.localtime(self.midday.end).time(), datetime.time(13, 0))
        self.assertEqual(timezone.localtime(self.morning.start).time(), datetime.time(8, 0))
        self.assertEqual(ResourcePlanning.objects.count(), 3)

    def test_insert_does_not_touch_other_resources_or_days(self):
        other_resource = self._make_planning(
            resource=(self.employee_ct, self.employee2.pk),
            date_from=self.date_from, date_to=self.date_from,
            t_start=datetime.time(10, 0), t_end=datetime.time(12, 0),
        )
        other_day = self._make_planning(
            date_from=datetime.date(2026, 7, 8), date_to=datetime.date(2026, 7, 8),
            t_start=datetime.time(10, 0), t_end=datetime.time(12, 0),
        )
        self._insert(datetime.time(10, 0), datetime.time(11, 0))

        other_resource.refresh_from_db()
        other_day.refresh_from_db()
        self.assertEqual(timezone.localtime(other_resource.start).time(), datetime.time(10, 0))
        self.assertEqual(timezone.localtime(other_day.start).time(), datetime.time(10, 0))

    def test_non_colliding_later_plan_stays_put(self):
        # Afternoon plan at 15:00 — the domino chain (midday pushed to 11–13)
        # never reaches it, so it must not move.
        afternoon = self._make_planning(
            date_from=self.date_from, date_to=self.date_from,
            t_start=datetime.time(15, 0), t_end=datetime.time(16, 0),
        )
        self._insert(datetime.time(10, 0), datetime.time(11, 0))

        afternoon.refresh_from_db()
        self.midday.refresh_from_db()
        self.assertEqual(timezone.localtime(afternoon.start).time(), datetime.time(15, 0))
        self.assertEqual(timezone.localtime(self.midday.start).time(), datetime.time(11, 0))

    def test_insert_fitting_in_a_gap_shifts_nothing(self):
        # Gap 12–14 before the afternoon plan; the new plan fits entirely.
        afternoon = self._make_planning(
            date_from=self.date_from, date_to=self.date_from,
            t_start=datetime.time(14, 0), t_end=datetime.time(16, 0),
        )
        self._insert(datetime.time(12, 0), datetime.time(13, 0))

        afternoon.refresh_from_db()
        self.midday.refresh_from_db()
        self.assertEqual(timezone.localtime(afternoon.start).time(), datetime.time(14, 0))
        self.assertEqual(timezone.localtime(self.midday.start).time(), datetime.time(10, 0))
        self.assertEqual(ResourcePlanning.objects.count(), 4)

    def test_insert_crossing_midnight_raises_and_saves_nothing(self):
        # Contiguous chain up to late evening: 12:00–23:30 follows the 10–12
        # plan, so the domino reaches it and would push it past midnight.
        evening = self._make_planning(
            date_from=self.date_from, date_to=self.date_from,
            t_start=datetime.time(12, 0), t_end=datetime.time(23, 30),
        )
        with self.assertRaises(ValidationError):
            self._insert(datetime.time(10, 0), datetime.time(11, 0))

        evening.refresh_from_db()
        self.midday.refresh_from_db()
        self.assertEqual(timezone.localtime(evening.start).time(), datetime.time(12, 0))
        self.assertEqual(timezone.localtime(evening.end).time(), datetime.time(23, 30))
        self.assertEqual(timezone.localtime(self.midday.start).time(), datetime.time(10, 0))
        self.assertEqual(ResourcePlanning.objects.count(), 3)  # nothing new created

    def test_without_shift_overlap_still_raises(self):
        with self.assertRaises(ValidationError):
            self._insert(datetime.time(10, 0), datetime.time(11, 0), shift=False)

    def test_cancelled_plans_are_not_shifted(self):
        self.midday.cancel()
        self._insert(datetime.time(10, 0), datetime.time(11, 0))
        self.midday.refresh_from_db()
        self.assertEqual(timezone.localtime(self.midday.start).time(), datetime.time(10, 0))


# ── Repetition (Apple-calendar style) ─────────────────────────────────────────

class TestRepeatPlannings(_Base):
    def _create(self, **overrides):
        """Monday 08–10, weekends excluded, unless overridden."""
        params = dict(
            date_from=datetime.date(2026, 7, 6),   # Monday
            date_to=datetime.date(2026, 7, 6),
            daily_end=datetime.time(10, 0),
            exclude_weekends=True,
        )
        params.update(overrides)
        return super()._create(**params)

    def test_no_repeat_creates_single_ungrouped_planning(self):
        plannings = self._create(repeat="never")
        self.assertEqual(len(plannings), 1)
        self.assertIsNone(plannings[0].group_id)

    def test_repeat_daily_until_date(self):
        # Mon 6th, daily until Wed 8th → 6, 7, 8 = 3 occurrences
        plannings = self._create(
            repeat="daily",
            repeat_until=datetime.date(2026, 7, 8),
        )
        self.assertEqual(len(plannings), 3)
        starts = sorted(timezone.localtime(p.start).date() for p in plannings)
        self.assertEqual(starts, [
            datetime.date(2026, 7, 6), datetime.date(2026, 7, 7), datetime.date(2026, 7, 8),
        ])

    def test_repeat_custom_every_2_days_until_date(self):
        # Mon 6th, every 2 days until Fri 10th → 6, 8, 10 = 3 occurrences
        plannings = self._create(
            repeat="custom",
            repeat_every_days=2,
            repeat_until=datetime.date(2026, 7, 10),
        )
        self.assertEqual(len(plannings), 3)
        starts = sorted(timezone.localtime(p.start).date() for p in plannings)
        self.assertEqual(starts, [
            datetime.date(2026, 7, 6), datetime.date(2026, 7, 8), datetime.date(2026, 7, 10),
        ])

    def test_custom_repeat_without_days_raises(self):
        with self.assertRaises(ValidationError):
            self._create(
                repeat="custom",
                repeat_every_days=None,
                repeat_until=datetime.date(2026, 7, 10),
            )

    def test_repeat_weekly_occurrences_share_one_group(self):
        # Mon 6th, weekly until 20th → 6, 13, 20 = 3 occurrences
        plannings = self._create(
            repeat="weekly",
            repeat_until=datetime.date(2026, 7, 20),
        )
        starts = sorted(timezone.localtime(p.start).date() for p in plannings)
        self.assertEqual(starts, [
            datetime.date(2026, 7, 6), datetime.date(2026, 7, 13), datetime.date(2026, 7, 20),
        ])
        group_ids = {p.group_id for p in plannings}
        self.assertEqual(len(group_ids), 1)
        group_id = group_ids.pop()
        self.assertIsNotNone(group_id)
        self.assertEqual(ResourcePlanningGroup.objects.count(), 1)
        self.assertTrue(ResourcePlanningGroup.objects.get(pk=group_id).is_repetition)

    def test_daily_repeat_skips_weekends_when_excluded(self):
        # Mon 6th, every day until Sun 12th with exclude_weekends → Mon–Fri = 5
        plannings = self._create(
            repeat="daily",
            repeat_until=datetime.date(2026, 7, 12),
        )
        self.assertEqual(len(plannings), 5)
        self.assertTrue(all(
            timezone.localtime(p.start).date().weekday() < 5 for p in plannings
        ))

    def test_weekend_only_repetition_raises(self):
        # Saturday, weekly, with exclude_weekends → every occurrence is weekend
        with self.assertRaises(ValidationError):
            self._create(
                date_from=datetime.date(2026, 7, 11), date_to=datetime.date(2026, 7, 11),
                repeat="weekly",
                repeat_until=datetime.date(2026, 7, 25),
            )

    def test_repeat_without_until_raises(self):
        with self.assertRaises(ValidationError):
            self._create(repeat="daily", repeat_until=None)

    def test_repeat_until_before_start_raises(self):
        with self.assertRaises(ValidationError):
            self._create(
                repeat="daily",
                repeat_until=datetime.date(2026, 7, 1),
            )

    def test_overlapping_occurrences_raise(self):
        # 3-day span (Mon–Wed) repeated every day → occurrences overlap each other
        with self.assertRaises(ValidationError):
            self._create(
                date_to=datetime.date(2026, 7, 8),
                repeat="daily",
                repeat_until=datetime.date(2026, 7, 10),
            )
        self.assertEqual(ResourcePlanning.objects.count(), 0)

    def test_repeat_conflicting_with_existing_plan_saves_nothing(self):
        self._make_planning(
            date_from=datetime.date(2026, 7, 13), date_to=datetime.date(2026, 7, 13),
            t_start=datetime.time(8, 0), t_end=datetime.time(10, 0),
        )
        with self.assertRaises(ValidationError):
            self._create(
                repeat="weekly",
                repeat_until=datetime.date(2026, 7, 20),
            )
        self.assertEqual(ResourcePlanning.objects.count(), 1)  # only the pre-existing
        self.assertEqual(ResourcePlanningGroup.objects.count(), 0)  # group rolled back

    def test_too_many_occurrences_raises(self):
        with self.assertRaises(ValidationError):
            self._create(
                repeat="daily",
                repeat_until=datetime.date(2026, 12, 31),
            )

    def test_repeat_combined_with_insert_mode_raises(self):
        with self.assertRaises(ValidationError):
            self._create(
                repeat="weekly",
                repeat_until=datetime.date(2026, 7, 20),
                shift_following=True,
            )


class TestRepeatInMultiPlannings(_Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.project_file2 = ProjectFile.objects.create(
            company=cls.company, reference="EXP-00002", name="Second project",
            status="open", client=cls.client_obj,
        )

    def test_repeating_line_creates_grouped_occurrences_only_for_that_line(self):
        lines = [
            {
                "project_file": self.project_file,
                "planning_type": self.planning_type,
                "date_from": datetime.date(2026, 7, 6),
                "date_to": datetime.date(2026, 7, 6),
                "daily_start": datetime.time(8, 0),
                "daily_end": datetime.time(10, 0),
                "exclude_weekends": True,
                "repeat": "weekly",
                "repeat_until": datetime.date(2026, 7, 20),
            },
            {
                "project_file": self.project_file2,
                "planning_type": self.planning_type,
                "date_from": datetime.date(2026, 7, 7),
                "date_to": datetime.date(2026, 7, 7),
                "daily_start": datetime.time(8, 0),
                "daily_end": datetime.time(10, 0),
                "exclude_weekends": True,
            },
        ]
        plannings = create_multi_plannings(
            company=self.company,
            status=ResourcePlanningStatus.PLANNED,
            resources=self.resources,
            lines=lines,
        )
        self.assertEqual(len(plannings), 4)  # 3 weekly + 1 single
        repeated = [p for p in plannings if p.project_file == self.project_file]
        single = [p for p in plannings if p.project_file == self.project_file2]
        self.assertEqual(len({p.group_id for p in repeated}), 1)
        self.assertIsNotNone(repeated[0].group_id)
        self.assertIsNone(single[0].group_id)
        self.assertEqual(ResourcePlanningGroup.objects.count(), 1)
        self.assertTrue(ResourcePlanningGroup.objects.first().is_repetition)
