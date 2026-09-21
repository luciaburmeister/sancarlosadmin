import unicodedata

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from company.models import Company
from configurator.models import Configuration, ProductTemplate
from production.models import ProductionItem, ProductionTicket


class ProductionItemGetStagePlanningTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Test Co")
        product = ProductTemplate.objects.create(
            name="Ventana", company=cls.company, production_stages=["corte"],
        )
        config = Configuration.objects.create(company=cls.company, product=product)
        ct = ContentType.objects.get_for_model(Configuration)
        ticket = ProductionTicket.objects.create(
            company=cls.company, content_type=ct, object_id=config.id,
        )
        cls.item = ProductionItem.objects.create(
            ticket=ticket, company=cls.company,
            planning={"corte": {"status": "pending"}},
        )

    def test_returns_planning_dict_for_matching_stage(self):
        self.assertEqual(self.item.get_stage_planning("corte"), {"status": "pending"})

    def test_matches_stage_name_regardless_of_unicode_normalization(self):
        # BOM stage names can arrive NFC or NFD-normalized depending on
        # source; a stored NFD key must still match an NFC lookup.
        stage_nfc = unicodedata.normalize("NFC", "reparación")
        stage_nfd = unicodedata.normalize("NFD", stage_nfc)
        self.assertNotEqual(stage_nfc, stage_nfd)

        nfd_item = ProductionItem.objects.create(
            ticket=self.item.ticket, company=self.company,
            planning={stage_nfd: {"status": "in_progress"}},
        )
        self.assertEqual(nfd_item.get_stage_planning(stage_nfc), {"status": "in_progress"})

    def test_returns_empty_dict_for_unknown_stage(self):
        self.assertEqual(self.item.get_stage_planning("montaje"), {})
