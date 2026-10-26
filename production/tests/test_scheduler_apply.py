from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from company.active_company import ActiveCompany
from company.models import CompanyUser
from production.models import PlannedProductionSession, SchedulingRun
from production.services.scheduler import ScheduleProposalApplier, proposal_to_dict
from production.services.scheduler.proposal import ScheduleProposal, ScheduledAssignment
from production.tests.factories import (
    STAGE_CUT, add_schedule, make_company, make_machine, make_session, make_ticket, resource_for,
)


class ScheduleProposalApplyTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        add_schedule(cls.company)
        cls.other_company = make_company(name="Other company")
        cls.oven = resource_for(make_machine(
            cls.company, name="Oven", stages=[STAGE_CUT], needs_human=False,
            parallel_capacity=2, daily_capacity_minutes=60,
        ))
        cls.ticket = make_ticket(
            cls.company, stages=[STAGE_CUT], stage_minutes={STAGE_CUT: 60},
            machine_qty=1, employee_qty=0,
        )
        cls.start = timezone.make_aware(datetime(2026, 9, 14, 9))
        cls.user = get_user_model().objects.create_user(username="planner", is_staff=True)
        CompanyUser.objects.create(user=cls.user, company=cls.company)

    def setUp(self):
        cache.clear()
        self.addCleanup(ActiveCompany.clear_current)

    def proposal(self, *, ticket=None, resource=None):
        ticket = ticket or self.ticket
        resource = resource or self.oven
        return ScheduleProposal(
            start_date=self.start.date(), end_date=self.start.date(), slot_minutes=60,
            solver_status="Optimal", is_optimal=True, objective_value=1, total_margin=1,
            selected_configuration_ids=[ticket.object_id],
            assignments=[ScheduledAssignment(
                configuration_id=ticket.object_id, configuration_name="Window",
                stage_name=STAGE_CUT, resource_id=resource.pk, resource_name="Oven",
                start=self.start, end=self.start + timedelta(hours=1), slot_count=1, minutes=60,
            )],
        )

    def busy_session(self):
        ticket = make_ticket(self.company, stages=[STAGE_CUT])
        return make_session(
            ticket, start=self.start, end=self.start + timedelta(hours=1), resources=[self.oven],
        )

    def test_parallel_oven_job_can_fill_the_remaining_place(self):
        self.busy_session()

        created = ScheduleProposalApplier().apply(self.proposal())

        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].production_ticket_id, self.ticket.pk)
        self.assertEqual(PlannedProductionSession.objects.count(), 2)

    def test_stale_proposal_cannot_overbook_oven_and_rolls_back_replacement(self):
        self.busy_session()
        second = make_ticket(self.company, stages=[STAGE_CUT])
        make_session(
            second, start=self.start, end=self.start + timedelta(hours=1), resources=[self.oven],
        )
        original = make_session(
            self.ticket, start=self.start + timedelta(hours=2),
            end=self.start + timedelta(hours=3), resources=[self.oven],
        )

        with self.assertRaises(ValidationError):
            ScheduleProposalApplier().apply(self.proposal())

        self.assertTrue(PlannedProductionSession.objects.filter(pk=original.pk).exists())
        self.assertEqual(PlannedProductionSession.objects.count(), 3)

    def test_other_company_resource_is_rejected(self):
        other_oven = resource_for(make_machine(
            self.other_company, name="Other oven", needs_human=False,
        ))

        with self.assertRaises(ValidationError):
            ScheduleProposalApplier().apply(self.proposal(resource=other_oven))

        self.assertFalse(PlannedProductionSession.objects.exists())

    def test_locked_session_is_preserved(self):
        original = make_session(
            self.ticket, start=self.start, end=self.start + timedelta(hours=1),
            resources=[self.oven], locked=True,
        )

        with self.assertRaises(ValidationError):
            ScheduleProposalApplier().apply(self.proposal())

        self.assertTrue(PlannedProductionSession.objects.filter(pk=original.pk, locked=True).exists())

    def apply_run(self, proposal, *, grant_permissions=True):
        if grant_permissions:
            self.user.user_permissions.add(*Permission.objects.filter(
                content_type__app_label="production",
                codename__in=[
                    "change_schedulingrun", "add_plannedproductionsession",
                    "delete_plannedproductionsession",
                ],
            ))
        run = SchedulingRun.objects.create(
            created_by=self.user, strategy="margin", status="solved",
            start_date=self.start.date(), end_date=self.start.date(), slot_minutes=60,
            result_payload=proposal_to_dict(proposal),
        )
        self.client.force_login(self.user)
        self.client.post(reverse("admin:company_switch_active_apply"), {"companies": [self.company.pk]})
        response = self.client.post(reverse("admin:production_schedulingrun_apply", args=[run.pk]))
        run.refresh_from_db()
        return response, run

    def test_apply_view_requires_planning_permissions(self):
        response, run = self.apply_run(self.proposal(), grant_permissions=False)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(run.status, "solved")
        self.assertFalse(PlannedProductionSession.objects.exists())

    def test_apply_view_checks_active_company_even_with_other_membership(self):
        CompanyUser.objects.create(user=self.user, company=self.other_company)
        other_ticket = make_ticket(self.other_company, stages=[STAGE_CUT])
        other_resource = resource_for(make_machine(self.other_company, name="Other oven"))

        response, run = self.apply_run(self.proposal(ticket=other_ticket, resource=other_resource))

        self.assertEqual(response.status_code, 403)
        self.assertEqual(run.status, "solved")
        self.assertFalse(PlannedProductionSession.objects.exists())

    def test_apply_view_reports_conflict_and_keeps_run_solved(self):
        original = make_session(
            self.ticket, start=self.start, end=self.start + timedelta(hours=1),
            resources=[self.oven], locked=True,
        )

        response, run = self.apply_run(self.proposal())

        self.assertEqual(response.status_code, 302)
        self.assertEqual(run.status, "solved")
        self.assertTrue(PlannedProductionSession.objects.filter(pk=original.pk).exists())

    def test_apply_view_saves_valid_proposal(self):
        response, run = self.apply_run(self.proposal())

        self.assertEqual(response.status_code, 302)
        self.assertEqual(run.status, "applied")
        self.assertEqual(PlannedProductionSession.objects.count(), 1)

        original_ids = set(PlannedProductionSession.objects.values_list("pk", flat=True))
        repeated = self.client.post(reverse("admin:production_schedulingrun_apply", args=[run.pk]))
        self.assertEqual(repeated.status_code, 302)
        self.assertEqual(set(PlannedProductionSession.objects.values_list("pk", flat=True)), original_ids)
