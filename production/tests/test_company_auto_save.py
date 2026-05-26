from django.test import TestCase
from django.contrib.contenttypes.models import ContentType
from company.models import Company
from configurator.models import ProductTemplate, Configuration
from production.models import ProductionTicket, ProductionItem, ProductionItemSession, ProductionItemSessionKind


class CompanyAutoSaveTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Test Company")
        self.product = ProductTemplate.objects.create(name="Test Product")
        self.config = Configuration.objects.create(
            company=self.company,
            product=self.product
        )
        self.ticket = ProductionTicket.objects.create(
            company=self.company,
            content_type=ContentType.objects.get_for_model(self.config),
            object_id=self.config.id
        )

    def test_production_item_auto_company(self):
        # Create ProductionItem without company
        item = ProductionItem.objects.create(
            ticket=self.ticket,
            # company is NOT set here
            data={"test": "data"}
        )
        # Verify company is auto-populated from ticket
        self.assertEqual(item.company, self.company)

    def test_production_item_session_auto_company(self):
        # Create ProductionItem first (will get company from ticket)
        item = ProductionItem.objects.create(
            ticket=self.ticket,
            data={"test": "data"}
        )

        # Create ProductionItemSession without company
        session = ProductionItemSession.objects.create(
            production_item=item,
            production_stage="Cutting",
            kind=ProductionItemSessionKind.ACTIVITY_STARTED
            # company is NOT set here
        )

        # Verify company is auto-populated from production_item
        self.assertEqual(session.company, self.company)
