"""A configuration with quantity N goes to production as N tickets, one per
piece (unit_index 1..N), each carrying the full per-piece BOM."""
from django.test import TestCase

from configurator.models import (
    Configuration,
    ConfigurationItem,
    ConfigurationStatus,
    ProductTemplate,
)
from materials.models import Component
from production.models import ProductionTicket, ProductionTicketStatus
from production.services.configuration_to_production import ConfigurationToProductionService
from production.tests.factories import STAGE_CUT, make_company


class ConfigurationQuantityTicketsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        cls.product = ProductTemplate.objects.create(
            name="Ventana", company=cls.company, production_stages=[STAGE_CUT],
        )
        cls.component = Component.objects.create(name="Perfil", unit_price_expression="1")

    def _config(self, quantity):
        config = Configuration.objects.create(
            company=self.company,
            product=self.product,
            name="Ventana",
            quantity=quantity,
            status=ConfigurationStatus.CONVERTED,
        )
        ConfigurationItem.objects.create(
            configuration=config,
            component=self.component,
            quantity=2,
            length=1,
            unit_price=0,
            is_template_item=True,
            production_stages=[STAGE_CUT],
        )
        return config

    def _tickets(self, config):
        return ProductionTicket.objects.filter(object_id=config.pk).order_by("unit_index")

    def _finish(self, ticket):
        for item in ticket.items.all():
            item.planning = {STAGE_CUT: {"status": "done"}}
            item.save(update_fields=["planning"])
        ticket.recompute_status()

    def test_one_ticket_per_piece_each_with_the_full_bom(self):
        config = self._config(quantity=3)

        tickets = ConfigurationToProductionService.create_tickets(config)

        self.assertEqual([t.unit_index for t in tickets], [1, 2, 3])
        self.assertEqual(self._tickets(config).count(), 3)
        for ticket in tickets:
            self.assertEqual(ticket.context["unit_count"], 3)
            self.assertEqual([float(i.quantity) for i in ticket.items.all()], [2.0])

    def test_create_ticket_returns_the_first_piece(self):
        config = self._config(quantity=2)

        ticket = ConfigurationToProductionService.create_ticket(config)

        self.assertEqual(ticket.unit_index, 1)
        self.assertEqual(self._tickets(config).count(), 2)

    def test_single_piece_label_is_unchanged(self):
        config = self._config(quantity=1)

        ticket = ConfigurationToProductionService.create_ticket(config)

        self.assertEqual(ticket.display_label, f"OP-{ticket.pk}")

    def test_multi_piece_label_names_the_piece(self):
        config = self._config(quantity=3)

        tickets = ConfigurationToProductionService.create_tickets(config)

        self.assertEqual(tickets[1].display_label, f"OP-{tickets[1].pk} (2/3)")

    def test_reconverting_with_more_pieces_adds_tickets(self):
        config = self._config(quantity=2)
        first = ConfigurationToProductionService.create_tickets(config)

        config.quantity = 4
        config.save(update_fields=["quantity"])
        tickets = ConfigurationToProductionService.create_tickets(config)

        self.assertEqual([t.pk for t in tickets[:2]], [t.pk for t in first])
        self.assertEqual(self._tickets(config).count(), 4)
        self.assertEqual(tickets[0].context["unit_count"], 4)

    def test_reconverting_with_fewer_pieces_cancels_the_surplus(self):
        config = self._config(quantity=3)
        ConfigurationToProductionService.create_tickets(config)

        config.quantity = 1
        config.save(update_fields=["quantity"])
        ConfigurationToProductionService.create_tickets(config)

        self.assertEqual(
            list(self._tickets(config).values_list("unit_index", "status")),
            [
                (1, ProductionTicketStatus.PENDING),
                (2, ProductionTicketStatus.CANCELLED),
                (3, ProductionTicketStatus.CANCELLED),
            ],
        )

    def test_configuration_is_production_done_only_when_every_piece_is(self):
        config = self._config(quantity=2)
        first, second = ConfigurationToProductionService.create_tickets(config)

        self._finish(first)
        config.refresh_from_db()
        self.assertEqual(config.status, ConfigurationStatus.CONVERTED)

        self._finish(second)
        config.refresh_from_db()
        self.assertEqual(config.status, ConfigurationStatus.PRODUCTION_DONE)
