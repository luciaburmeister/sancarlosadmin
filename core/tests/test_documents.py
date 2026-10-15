"""Admin downloads rendered on the pdf queue (core.documents)."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.storage import default_storage
from django.test import RequestFactory, TestCase
from django.urls import reverse

from core.documents import document_renderer, enqueue_document, load_renderer, renderer_path
from core.tasks import render_document_task


@document_renderer
def fake_pdf(object_id: int):
    return b"%PDF-1.4 " + str(object_id).encode()


@document_renderer
def nothing(object_id: int):
    return None


def not_a_renderer():
    return b"x"


class DocumentsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(username="docs", email="d@d.com", password="x")
        self.client.force_login(self.user)

    def test_enqueue_redirects_to_the_status_page_with_the_task_id(self):
        request = RequestFactory().get("/")
        request.user = self.user
        with patch("core.tasks.render_document_task.delay") as delay:
            delay.return_value.id = "task-1"
            response = enqueue_document(
                request, fake_pdf, object_id=7, filename="doc.pdf", back_url="/admin/sales/quote/7/change/",
            )
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("core_document_status"), response.url)
        self.assertIn("task_id=task-1", response.url)
        self.assertIn("back_url=%2Fadmin%2Fsales%2Fquote%2F7%2Fchange%2F", response.url)
        delay.assert_called_once_with(
            renderer=renderer_path(fake_pdf), kwargs={"object_id": 7}, filename="doc.pdf",
            content_type="application/pdf", inline=False, user_id=self.user.pk,
        )

    def test_only_decorated_functions_can_be_loaded(self):
        self.assertIs(load_renderer(renderer_path(fake_pdf)), fake_pdf)
        with self.assertRaises(ValueError):
            load_renderer(f"{__name__}.not_a_renderer")
        with self.assertRaises(TypeError):
            renderer_path(not_a_renderer)

    def test_task_stores_the_document_and_the_download_serves_it_once(self):
        result = render_document_task(
            renderer=renderer_path(fake_pdf), kwargs={"object_id": 3}, filename="Presupuesto 3.pdf",
            inline=True, user_id=self.user.pk,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["filename"], "Presupuesto_3.pdf")
        self.assertTrue(default_storage.exists(result["storage_path"]))

        with patch("core.views.document_status.AsyncResult") as async_result:
            async_result.return_value.state = "SUCCESS"
            async_result.return_value.result = result
            status = self.client.get(reverse("core_document_status_api", args=["t"])).json()
            response = self.client.get(reverse("core_document_download", args=["t"]))

        self.assertEqual(status["state"], "done")
        self.assertEqual(status["download_url"], reverse("core_document_download", args=["t"]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"%PDF-1.4 3")
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(response["Content-Disposition"], 'inline; filename="Presupuesto_3.pdf"')
        self.assertFalse(default_storage.exists(result["storage_path"]))

    def test_download_is_only_for_its_owner(self):
        other = get_user_model().objects.create_user(username="other", password="x", is_staff=True)
        self.client.force_login(other)
        with patch("core.views.document_status.AsyncResult") as async_result:
            async_result.return_value.state = "SUCCESS"
            async_result.return_value.result = {
                "status": "ok", "storage_path": "tmp/documents/x.pdf", "filename": "x.pdf", "user_id": self.user.pk,
            }
            response = self.client.get(reverse("core_document_download", args=["t"]))
        self.assertEqual(response.status_code, 403)

    def test_a_renderer_with_nothing_to_render_is_reported_as_an_error(self):
        result = render_document_task(renderer=renderer_path(nothing), kwargs={"object_id": 1}, filename="x.pdf")
        self.assertEqual(result["status"], "empty")
        with patch("core.views.document_status.AsyncResult") as async_result:
            async_result.return_value.state = "SUCCESS"
            async_result.return_value.result = result
            status = self.client.get(reverse("core_document_status_api", args=["t"])).json()
        self.assertEqual(status["state"], "error")

    def test_status_page_and_api_need_a_staff_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("core_document_status")).status_code, 302)
        self.assertEqual(self.client.get(reverse("core_document_status_api", args=["t"])).status_code, 302)

    def test_status_page_renders_in_the_admin_shell(self):
        response = self.client.get(reverse("core_document_status") + "?task_id=abc&back_url=/admin/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("core_document_status_api", args=["abc"]))
