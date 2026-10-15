import unicodedata

from django.test import TestCase

from production.models import ProductionItem
from production.tests.factories import make_company, make_item, make_product, make_ticket


class ProductionItemGetStagePlanningTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(stages=[])
        product = make_product(cls.company, name="Ventana", stages=["corte"])
        ticket = make_ticket(cls.company, stages=["corte"], product=product, bom=False)
        cls.item = make_item(
            ticket, stages=["corte"], planning={"corte": {"status": "pending"}}, production_stages_order=[],
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
