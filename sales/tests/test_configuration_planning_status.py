"""
Tests for the stored planning/production status fields.

Covers:
- Signal sync: creating/deleting PlannedProductionSession rows flips the
  source configuration between planned/unplanned (including queryset deletes,
  the path used by ScheduleProposalApplier).
- The denormalized Quote.production_status kept in sync from configuration
  changes and planning changes.
- The quote admin production badge states.
- The quote admin filters (includes production / production scheduling).
"""
from datetime import timedelta

from django.contrib import admin
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.utils import timezone

from company.models import Company
from configurator.models import (
    Configuration,
    ConfigurationPlanningStatus,
    ConfigurationStatus,
    ProductTemplate,
)
from production.models import PlannedProductionSession, ProductionTicket
from sales.admin.quote import (
    PRODUCTION_BADGE_NONE,
    PRODUCTION_BADGE_NOT_CONVERTED,
    PRODUCTION_BADGE_PENDING,
    PRODUCTION_BADGE_SCHEDULED,
    PRODUCTION_BADGE_UNSCHEDULED,
    QuoteAdmin,
)
from sales.admin.filters import HasProductionFilter, ProductionSchedulingFilter
from sales.models import Quote, QuoteProductionStatus, QuoteStatus
from sales.tests.factories import make_company, make_configuration, make_quote


class _PlanningBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(name="Planning Co")
        cls.product = ProductTemplate.objects.create(
            name="Planned Product",
            company=cls.company,
        )

    def _make_quote(self, status=QuoteStatus.DRAFT):
        return make_quote(company=self.company, project_name=f"Quote {status}", status=status)

    def _make_config(self, quote, status=ConfigurationStatus.DRAFT):
        return make_configuration(quote=quote, product=self.product, name=f"Config {status}", status=status)

    def _make_ticket(self, config):
        return ProductionTicket.objects.create(
            company=self.company,
            content_type=ContentType.objects.get_for_model(Configuration),
            object_id=config.pk,
        )

    def _make_session(self, ticket, stage="cut"):
        start = timezone.now()
        return PlannedProductionSession.objects.create(
            production_ticket=ticket,
            production_stage=stage,
            planned_start=start,
            planned_end=start + timedelta(hours=1),
        )


class PlanningStatusSyncTests(_PlanningBase):
    def test_creating_session_marks_configuration_planned(self):
        config = self._make_config(self._make_quote(), ConfigurationStatus.CONVERTED)
        ticket = self._make_ticket(config)

        self.assertEqual(config.planning_status, ConfigurationPlanningStatus.UNPLANNED)
        self._make_session(ticket)

        config.refresh_from_db()
        self.assertEqual(config.planning_status, ConfigurationPlanningStatus.PLANNED)
        self.assertIsNotNone(config.planned_at)

    def test_deleting_last_session_marks_configuration_unplanned(self):
        config = self._make_config(self._make_quote(), ConfigurationStatus.CONVERTED)
        ticket = self._make_ticket(config)
        first = self._make_session(ticket, "cut")
        second = self._make_session(ticket, "weld")

        first.delete()
        config.refresh_from_db()
        self.assertEqual(config.planning_status, ConfigurationPlanningStatus.PLANNED)

        second.delete()
        config.refresh_from_db()
        self.assertEqual(config.planning_status, ConfigurationPlanningStatus.UNPLANNED)
        self.assertIsNone(config.planned_at)

    def test_queryset_delete_unsets_planning_status(self):
        # The scheduler applier deletes sessions with a queryset .delete().
        config = self._make_config(self._make_quote(), ConfigurationStatus.CONVERTED)
        ticket = self._make_ticket(config)
        self._make_session(ticket, "cut")
        self._make_session(ticket, "weld")

        PlannedProductionSession.objects.filter(production_ticket=ticket).delete()

        config.refresh_from_db()
        self.assertEqual(config.planning_status, ConfigurationPlanningStatus.UNPLANNED)

    def test_session_on_non_configuration_source_is_ignored(self):
        ticket = ProductionTicket.objects.create(
            company=self.company,
            content_type=ContentType.objects.get_for_model(Company),
            object_id=self.company.pk,
        )
        # Must not raise.
        self._make_session(ticket)


class QuoteProductionStatusSyncTests(_PlanningBase):
    def _status(self, quote):
        quote.refresh_from_db()
        return quote.production_status

    def test_adding_and_removing_configuration(self):
        quote = self._make_quote()
        self.assertEqual(self._status(quote), QuoteProductionStatus.NO_PRODUCTION)

        config = self._make_config(quote, ConfigurationStatus.DRAFT)
        self.assertEqual(self._status(quote), QuoteProductionStatus.NOT_CONVERTED)

        config.delete()
        self.assertEqual(self._status(quote), QuoteProductionStatus.NO_PRODUCTION)

    def test_configuration_status_transitions(self):
        quote = self._make_quote()
        config = self._make_config(quote, ConfigurationStatus.COMPLETED)
        self.assertEqual(self._status(quote), QuoteProductionStatus.NOT_CONVERTED)

        config.status = ConfigurationStatus.CONVERTED
        config.save()
        self.assertEqual(self._status(quote), QuoteProductionStatus.UNSCHEDULED)

        config.status = ConfigurationStatus.CANCELLED
        config.save()
        self.assertEqual(self._status(quote), QuoteProductionStatus.NO_PRODUCTION)

    def test_planning_sessions_drive_quote_status(self):
        quote = self._make_quote()
        config = self._make_config(quote, ConfigurationStatus.CONVERTED)
        ticket = self._make_ticket(config)
        self.assertEqual(self._status(quote), QuoteProductionStatus.UNSCHEDULED)

        session = self._make_session(ticket)
        self.assertEqual(self._status(quote), QuoteProductionStatus.SCHEDULED)

        session.delete()
        self.assertEqual(self._status(quote), QuoteProductionStatus.UNSCHEDULED)

    def test_reassigning_configuration_updates_both_quotes(self):
        quote_a = self._make_quote()
        quote_b = self._make_quote()
        config = self._make_config(quote_a, ConfigurationStatus.DRAFT)
        self.assertEqual(self._status(quote_a), QuoteProductionStatus.NOT_CONVERTED)
        self.assertEqual(self._status(quote_b), QuoteProductionStatus.NO_PRODUCTION)

        config.quote = quote_b
        config.save()
        self.assertEqual(self._status(quote_a), QuoteProductionStatus.NO_PRODUCTION)
        self.assertEqual(self._status(quote_b), QuoteProductionStatus.NOT_CONVERTED)


class QuoteProductionBadgeTests(_PlanningBase):
    def _badge(self, quote):
        quote_admin = QuoteAdmin(Quote, admin.site)
        obj = Quote.objects.get(pk=quote.pk)
        return quote_admin.production_planning_badge(obj)

    def test_no_configurations(self):
        quote = self._make_quote()
        self.assertEqual(self._badge(quote), PRODUCTION_BADGE_NONE)

    def test_cancelled_configurations_do_not_count(self):
        quote = self._make_quote()
        self._make_config(quote, ConfigurationStatus.CANCELLED)
        self.assertEqual(self._badge(quote), PRODUCTION_BADGE_NONE)

    def test_not_accepted_quote_shows_production(self):
        quote = self._make_quote(QuoteStatus.DRAFT)
        self._make_config(quote, ConfigurationStatus.DRAFT)
        self.assertEqual(self._badge(quote), PRODUCTION_BADGE_PENDING)

    def test_accepted_with_unconverted_configuration(self):
        quote = self._make_quote(QuoteStatus.ACCEPTED)
        self._make_config(quote, ConfigurationStatus.COMPLETED)
        self.assertEqual(self._badge(quote), PRODUCTION_BADGE_NOT_CONVERTED)

    def test_accepted_converted_but_unplanned(self):
        quote = self._make_quote(QuoteStatus.ACCEPTED)
        self._make_config(quote, ConfigurationStatus.CONVERTED)
        self.assertEqual(self._badge(quote), PRODUCTION_BADGE_UNSCHEDULED)

    def test_accepted_and_fully_planned(self):
        quote = self._make_quote(QuoteStatus.ACCEPTED)
        config = self._make_config(quote, ConfigurationStatus.CONVERTED)
        self._make_session(self._make_ticket(config))
        self.assertEqual(self._badge(quote), PRODUCTION_BADGE_SCHEDULED)

    def test_partially_planned_quote_is_unscheduled(self):
        quote = self._make_quote(QuoteStatus.ACCEPTED)
        planned = self._make_config(quote, ConfigurationStatus.CONVERTED)
        self._make_config(quote, ConfigurationStatus.CONVERTED)
        self._make_session(self._make_ticket(planned))
        self.assertEqual(self._badge(quote), PRODUCTION_BADGE_UNSCHEDULED)


class QuoteProductionFilterTests(_PlanningBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.quote_no_production = make_quote(company=cls.company, project_name="No production")
        cls.quote_pending = make_quote(company=cls.company, project_name="Pending", status=QuoteStatus.ACCEPTED)
        make_configuration(
            quote=cls.quote_pending, product=cls.product, name="unconverted", status=ConfigurationStatus.COMPLETED,
        )
        cls.quote_unscheduled = make_quote(company=cls.company, project_name="Unscheduled", status=QuoteStatus.ACCEPTED)
        make_configuration(
            quote=cls.quote_unscheduled, product=cls.product, name="converted-unplanned", status=ConfigurationStatus.CONVERTED,
        )
        cls.quote_scheduled = make_quote(company=cls.company, project_name="Scheduled", status=QuoteStatus.ACCEPTED)
        make_configuration(
            quote=cls.quote_scheduled, product=cls.product, name="planned", status=ConfigurationStatus.CONVERTED,
            planning_status=ConfigurationPlanningStatus.PLANNED,
        )

    def _filtered(self, filter_class, value):
        f = filter_class(
            request=None,
            params={filter_class.parameter_name: [value]},
            model=Quote,
            model_admin=None,
        )
        return set(f.queryset(None, Quote.objects.all()))

    def test_has_production_yes(self):
        self.assertEqual(
            self._filtered(HasProductionFilter, "yes"),
            {self.quote_pending, self.quote_unscheduled, self.quote_scheduled},
        )

    def test_has_production_no(self):
        self.assertEqual(
            self._filtered(HasProductionFilter, "no"),
            {self.quote_no_production},
        )

    def test_scheduling_not_converted(self):
        self.assertEqual(
            self._filtered(ProductionSchedulingFilter, "not_converted"),
            {self.quote_pending},
        )

    def test_scheduling_unscheduled(self):
        self.assertEqual(
            self._filtered(ProductionSchedulingFilter, "unscheduled"),
            {self.quote_pending, self.quote_unscheduled},
        )

    def test_scheduling_scheduled(self):
        self.assertEqual(
            self._filtered(ProductionSchedulingFilter, "scheduled"),
            {self.quote_scheduled},
        )

    def test_needs_attention(self):
        self.assertEqual(
            self._filtered(ProductionSchedulingFilter, "needs_attention"),
            {self.quote_pending, self.quote_unscheduled},
        )
