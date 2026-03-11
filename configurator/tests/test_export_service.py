from django.test import TestCase
from unittest.mock import MagicMock
from configurator.models import Configuration
from production.models import ProductionItem, ProductionTicket
from configurator.services.export import (
    ConfigurationExportService,
    ConfigurationExportStrategy,
    ProductionItemExportStrategy
)

class ExportServiceStrategyTest(TestCase):
    def test_get_strategy_for_configuration(self):
        config = MagicMock(spec=Configuration)
        strategy = ConfigurationExportService.get_strategy(config)
        self.assertIsInstance(strategy, ConfigurationExportStrategy)

    def test_get_strategy_for_production_item(self):
        item = MagicMock(spec=ProductionItem)
        strategy = ConfigurationExportService.get_strategy(item)
        self.assertIsInstance(strategy, ProductionItemExportStrategy)

    def test_strategy_context_contains_item_for_production_item(self):
        item = MagicMock(spec=ProductionItem)
        item.data = {"test": "data"}
        item.planning = {}
        item.ticket = MagicMock(spec=ProductionTicket)
        item.ticket.source_object = None
        
        strategy = ProductionItemExportStrategy(item)
        context = strategy.get_context()
        
        self.assertIn("item", context)
        self.assertEqual(context["item"], item)
        self.assertEqual(context["data"], item.data)
        self.assertEqual(context["ticket"], item.ticket)
