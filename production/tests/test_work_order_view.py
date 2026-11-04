"""The work order and drawings pages show each document without one hiding another."""
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from company.active_company import ActiveCompany
from production.tests.factories import make_company, make_staff_employee, make_ticket

PERMS = ["view_productionticket"]
TEMPLATE_HTML = "<html><body><p>TEMPLATE DATA</p></body></html>"


def upload(name, content_type="application/pdf"):
    return SimpleUploadedFile(name, b"content", content_type=content_type)


class TicketDocumentViewsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(name="Alpha")
        cls.other_company = make_company(name="Beta")
        cls.employee = make_staff_employee(cls.company, username="viewer", perms=PERMS)

        cls.template_ticket = make_ticket(cls.company, bom=False, order_html=TEMPLATE_HTML)
        cls.drawings_ticket = make_ticket(
            cls.company, bom=False, order_html=TEMPLATE_HTML, drawings_file=upload("plano.pdf"),
        )
        cls.image_drawings_ticket = make_ticket(
            cls.company, bom=False, drawings_file=upload("plano.png", "image/png"),
        )
        cls.supplier_ticket = make_ticket(
            cls.company,
            bom=False,
            order_html=TEMPLATE_HTML,
            work_order_pdf=upload("work_order_1.pdf"),
            drawings_file=upload("plano.pdf"),
        )
        cls.foreign_ticket = make_ticket(
            cls.other_company, bom=False, order_html=TEMPLATE_HTML, drawings_file=upload("plano.pdf"),
        )

    def setUp(self):
        self.addCleanup(ActiveCompany.clear_current)
        self.client.force_login(self.employee.user)
        self.client.post(reverse("admin:company_switch_active_apply"), {"companies": [self.company.pk]})

    def _url(self, name, ticket):
        return reverse(f"admin:production_productionticket_{name}", args=[ticket.pk])

    def test_template_work_order_keeps_its_data_and_links_to_drawings(self):
        response = self.client.get(self._url("order_html", self.drawings_ticket))

        self.assertContains(response, "TEMPLATE DATA")
        self.assertContains(response, self._url("drawings", self.drawings_ticket))
        self.assertContains(response, self._url("stages", self.drawings_ticket))

    def test_work_order_has_no_drawings_link_without_drawings(self):
        response = self.client.get(self._url("order_html", self.template_ticket))

        self.assertContains(response, "TEMPLATE DATA")
        self.assertNotContains(response, self._url("drawings", self.template_ticket))

    def test_supplier_pdf_still_replaces_the_template(self):
        response = self.client.get(self._url("order_html", self.supplier_ticket))

        self.assertContains(response, f'<iframe src="{self.supplier_ticket.work_order_pdf.url}"')
        self.assertNotContains(response, "TEMPLATE DATA")
        self.assertContains(response, self._url("drawings", self.supplier_ticket))

    def test_pdf_drawings_page_embeds_the_file_and_links_back(self):
        response = self.client.get(self._url("drawings", self.drawings_ticket))

        self.assertContains(response, f'<iframe src="{self.drawings_ticket.drawings_file.url}"')
        self.assertContains(response, self._url("order_html", self.drawings_ticket))
        self.assertContains(response, self._url("stages", self.drawings_ticket))

    def test_image_drawings_page_shows_the_image(self):
        response = self.client.get(self._url("drawings", self.image_drawings_ticket))

        self.assertContains(response, f'<img src="{self.image_drawings_ticket.drawings_file.url}"')
        self.assertNotContains(response, "<iframe")

    def test_drawings_page_is_not_found_without_drawings(self):
        response = self.client.get(self._url("drawings", self.template_ticket))

        self.assertEqual(response.status_code, 404)

    def test_other_company_documents_are_not_found(self):
        for name in ("order_html", "drawings"):
            with self.subTest(page=name):
                response = self.client.get(self._url(name, self.foreign_ticket))
                self.assertEqual(response.status_code, 404)

    def test_stages_page_links_to_drawings_page(self):
        with_drawings = self.client.get(self._url("stages", self.drawings_ticket))
        without_drawings = self.client.get(self._url("stages", self.template_ticket))

        self.assertContains(with_drawings, self._url("drawings", self.drawings_ticket))
        self.assertNotContains(without_drawings, self._url("drawings", self.template_ticket))

    def test_drawings_reject_unsupported_file_types(self):
        ticket = make_ticket(self.company, bom=False)
        ticket.drawings_file = SimpleUploadedFile("plano.exe", b"MZ")

        with self.assertRaises(ValidationError) as raised:
            ticket.full_clean()
        self.assertIn("drawings_file", raised.exception.message_dict)
