"""Derived delivery / production status on ProjectFile and the lifecycle
transitions they drive (see management.services.project_file_status)."""
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from company.models import Company
from configurator.models import Configuration, ConfigurationStatus, ProductTemplate
from management.models import (
    ProjectFile,
    ProjectFileDeliveryStatus,
    ProjectFileProductionStatus,
    ProjectFileStatus,
)
from production.models import ProductionItem, ProductionItemStatus, ProductionTicket, ProductionTicketStatus
from sales.models import (
    Client,
    DeliveryOrder,
    DeliveryOrderItem,
    DeliveryOrderStatus,
    Quote,
    QuoteStatus,
)


class _ProjectFileStatusBase(TestCase):
    STAGE = "corte"

    def setUp(self):
        self.company = Company.objects.create(name="Test Co", production_stages=[self.STAGE])
        self.client_obj = Client.objects.create(name="Acme", company=self.company)
        self.product = ProductTemplate.objects.create(
            name="Door", company=self.company, production_stages=[self.STAGE],
        )
        self.project_file = ProjectFile.objects.create(
            name="PF", company=self.company, client=self.client_obj,
        )
        self.config_ct = ContentType.objects.get_for_model(Configuration)

    def _accepted_quote(self):
        return Quote.objects.create(
            project_name="Quote", company=self.company, client=self.client_obj,
            project_file=self.project_file, status=QuoteStatus.ACCEPTED,
        )

    def _config(self, quote, units="2"):
        return Configuration.objects.create(
            name="Config", company=self.company, product=self.product,
            client=self.client_obj, quote=quote, project_file=self.project_file,
            status=ConfigurationStatus.COMPLETED, units=Decimal(units),
        )

    def _delivery_order(self, status=DeliveryOrderStatus.SENT, project_file=None):
        return DeliveryOrder.objects.create(
            company=self.company, client=self.client_obj,
            project_file=project_file or self.project_file, status=status,
        )

    def _line(self, do, config, quantity):
        return DeliveryOrderItem.objects.create(
            delivery_order=do, description=config.name, quantity=Decimal(quantity),
            unit_price=Decimal("10.00"), source_content_type=self.config_ct,
            source_object_id=config.pk,
        )

    def _ticket(self, config, stage_status=None):
        ticket = ProductionTicket.objects.create(
            company=self.company, content_type=self.config_ct, object_id=config.pk,
            project_file=self.project_file,
        )
        planning = {self.STAGE: {"status": stage_status}} if stage_status else {}
        ProductionItem.objects.create(
            ticket=ticket, company=self.company, planning=planning,
            production_stages_order=[self.STAGE],
        )
        return ticket

    def _set_stage(self, ticket, status):
        item = ticket.items.get()
        item.planning = {self.STAGE: {"status": status}}
        item.save()

    def pf(self):
        return ProjectFile.objects.get(pk=self.project_file.pk)


class DeliveryStatusTests(_ProjectFileStatusBase):

    def test_defaults_to_not_applicable_without_deliverables(self):
        self.project_file.recompute_status()
        self.assertEqual(self.pf().delivery_status, ProjectFileDeliveryStatus.NOT_APPLICABLE)

    def test_accepted_quote_without_deliveries_is_not_delivered(self):
        self._config(self._accepted_quote())
        self.project_file.recompute_status()
        self.assertEqual(self.pf().delivery_status, ProjectFileDeliveryStatus.NOT_DELIVERED)

    def test_partial_then_full_delivery_moves_automatically(self):
        config = self._config(self._accepted_quote(), units="2")
        do = self._delivery_order()
        self._line(do, config, "1")
        self.assertEqual(self.pf().delivery_status, ProjectFileDeliveryStatus.PARTIALLY_DELIVERED)

        self._line(self._delivery_order(), config, "1")
        self.assertEqual(self.pf().delivery_status, ProjectFileDeliveryStatus.DELIVERED)

    def test_draft_orders_do_not_count_until_sent(self):
        config = self._config(self._accepted_quote(), units="1")
        do = self._delivery_order(status=DeliveryOrderStatus.DRAFT)
        self._line(do, config, "1")
        self.assertEqual(self.pf().delivery_status, ProjectFileDeliveryStatus.NOT_DELIVERED)

        do.status = DeliveryOrderStatus.SENT
        do.save()
        self.assertEqual(self.pf().delivery_status, ProjectFileDeliveryStatus.DELIVERED)

    def test_cancelling_order_reverts_delivery_status(self):
        config = self._config(self._accepted_quote(), units="1")
        do = self._delivery_order()
        self._line(do, config, "1")
        self.assertEqual(self.pf().delivery_status, ProjectFileDeliveryStatus.DELIVERED)

        do.status = DeliveryOrderStatus.CANCELLED
        do.save()
        self.assertEqual(self.pf().delivery_status, ProjectFileDeliveryStatus.NOT_DELIVERED)

    def test_free_text_line_without_source_counts_as_partial(self):
        self._config(self._accepted_quote(), units="1")
        do = self._delivery_order()
        DeliveryOrderItem.objects.create(
            delivery_order=do, description="Extra", quantity=Decimal("1"),
            unit_price=Decimal("5.00"),
        )
        self.assertEqual(self.pf().delivery_status, ProjectFileDeliveryStatus.PARTIALLY_DELIVERED)

    def test_hand_built_order_without_deliverables_is_delivered(self):
        do = self._delivery_order()
        DeliveryOrderItem.objects.create(
            delivery_order=do, description="Extra", quantity=Decimal("1"),
            unit_price=Decimal("5.00"),
        )
        self.assertEqual(self.pf().delivery_status, ProjectFileDeliveryStatus.DELIVERED)

    def test_moving_order_to_another_expediente_recomputes_both(self):
        config = self._config(self._accepted_quote(), units="1")
        do = self._delivery_order()
        self._line(do, config, "1")
        other = ProjectFile.objects.create(name="Other", company=self.company, client=self.client_obj)

        do.project_file = other
        do.save()
        self.assertEqual(self.pf().delivery_status, ProjectFileDeliveryStatus.NOT_DELIVERED)
        other.refresh_from_db()
        self.assertEqual(other.delivery_status, ProjectFileDeliveryStatus.DELIVERED)

    def test_deleting_line_recomputes(self):
        config = self._config(self._accepted_quote(), units="1")
        line = self._line(self._delivery_order(), config, "1")
        self.assertEqual(self.pf().delivery_status, ProjectFileDeliveryStatus.DELIVERED)
        line.delete()
        self.assertEqual(self.pf().delivery_status, ProjectFileDeliveryStatus.NOT_DELIVERED)


class ProductionStatusTests(_ProjectFileStatusBase):

    def test_no_tickets_is_no_production(self):
        self.project_file.recompute_status()
        self.assertEqual(self.pf().production_status, ProjectFileProductionStatus.NO_PRODUCTION)

    def test_ticket_lifecycle_rolls_up(self):
        config = self._config(self._accepted_quote())
        ticket = self._ticket(config)
        self.assertEqual(self.pf().production_status, ProjectFileProductionStatus.PENDING)

        self._set_stage(ticket, ProductionItemStatus.IN_PROGRESS)
        self.assertEqual(self.pf().production_status, ProjectFileProductionStatus.IN_PROGRESS)

        self._set_stage(ticket, ProductionItemStatus.DONE)
        self.assertEqual(self.pf().production_status, ProjectFileProductionStatus.DONE)

    def test_done_requires_every_ticket(self):
        quote = self._accepted_quote()
        t1 = self._ticket(self._config(quote), stage_status=ProductionItemStatus.DONE)
        t1.recompute_status()
        self._ticket(self._config(quote))
        self.assertEqual(self.pf().production_status, ProjectFileProductionStatus.IN_PROGRESS)

    def test_cancelled_tickets_are_ignored(self):
        quote = self._accepted_quote()
        t1 = self._ticket(self._config(quote), stage_status=ProductionItemStatus.DONE)
        t1.recompute_status()
        t2 = self._ticket(self._config(quote))
        t2.status = ProductionTicketStatus.CANCELLED
        t2.save(update_fields=["status"])
        self.assertEqual(self.pf().production_status, ProjectFileProductionStatus.DONE)

    def test_legacy_ticket_linked_through_quote(self):
        config = self._config(self._accepted_quote())
        ticket = ProductionTicket.objects.create(
            company=self.company, content_type=self.config_ct, object_id=config.pk,
        )
        ProductionItem.objects.create(
            ticket=ticket, company=self.company,
            planning={self.STAGE: {"status": ProductionItemStatus.IN_PROGRESS}},
            production_stages_order=[self.STAGE],
        )
        self.assertEqual(self.pf().production_status, ProjectFileProductionStatus.IN_PROGRESS)


class LifecycleTransitionTests(_ProjectFileStatusBase):

    def test_new_becomes_active_when_production_starts(self):
        ticket = self._ticket(self._config(self._accepted_quote()))
        self.assertEqual(self.pf().status, ProjectFileStatus.NEW)
        self._set_stage(ticket, ProductionItemStatus.IN_PROGRESS)
        self.assertEqual(self.pf().status, ProjectFileStatus.ACTIVE)

    def test_new_becomes_active_when_something_ships(self):
        config = self._config(self._accepted_quote(), units="2")
        self._line(self._delivery_order(), config, "1")
        self.assertEqual(self.pf().status, ProjectFileStatus.ACTIVE)

    def test_active_completes_only_when_delivered_produced_and_invoiced(self):
        config = self._config(self._accepted_quote(), units="1")
        ticket = self._ticket(config, stage_status=ProductionItemStatus.DONE)
        ticket.recompute_status()
        self._line(self._delivery_order(), config, "1")
        # Delivered + produced but not invoiced: stays active.
        self.assertEqual(self.pf().status, ProjectFileStatus.ACTIVE)

        ProjectFile.objects.filter(pk=self.project_file.pk).update(
            total_amount=Decimal("100"), percentage_invoiced=Decimal("100"),
        )
        self.project_file.recompute_status()
        self.assertEqual(self.pf().status, ProjectFileStatus.COMPLETED)

    def test_on_hold_and_cancelled_are_never_touched(self):
        config = self._config(self._accepted_quote(), units="1")
        for frozen in (ProjectFileStatus.ON_HOLD, ProjectFileStatus.CANCELLED):
            ProjectFile.objects.filter(pk=self.project_file.pk).update(status=frozen)
            self._line(self._delivery_order(), config, "1")
            self.assertEqual(self.pf().status, frozen)

    def test_completed_never_reverts_automatically(self):
        config = self._config(self._accepted_quote(), units="1")
        do = self._delivery_order()
        self._line(do, config, "1")
        ProjectFile.objects.filter(pk=self.project_file.pk).update(status=ProjectFileStatus.COMPLETED)
        do.status = DeliveryOrderStatus.CANCELLED
        do.save()
        self.assertEqual(self.pf().status, ProjectFileStatus.COMPLETED)
        self.assertEqual(self.pf().delivery_status, ProjectFileDeliveryStatus.NOT_DELIVERED)


class OperationalStatusTests(_ProjectFileStatusBase):

    def test_matrix(self):
        pf = self.project_file
        cases = [
            (ProjectFileProductionStatus.PENDING, ProjectFileDeliveryStatus.NOT_DELIVERED, "in_production"),
            (ProjectFileProductionStatus.IN_PROGRESS, ProjectFileDeliveryStatus.NOT_DELIVERED, "in_production"),
            (ProjectFileProductionStatus.DONE, ProjectFileDeliveryStatus.NOT_DELIVERED, "ready_to_deliver"),
            (ProjectFileProductionStatus.NO_PRODUCTION, ProjectFileDeliveryStatus.NOT_DELIVERED, "ready_to_deliver"),
            (ProjectFileProductionStatus.IN_PROGRESS, ProjectFileDeliveryStatus.PARTIALLY_DELIVERED, "partially_delivered"),
            (ProjectFileProductionStatus.DONE, ProjectFileDeliveryStatus.DELIVERED, "delivered"),
            (ProjectFileProductionStatus.NO_PRODUCTION, ProjectFileDeliveryStatus.NOT_APPLICABLE, "nothing_pending"),
        ]
        for production, delivery, expected in cases:
            pf.production_status = production
            pf.delivery_status = delivery
            self.assertEqual(pf.operational_status[0], expected, (production, delivery))
