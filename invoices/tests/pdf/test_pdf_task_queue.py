"""
Every task that renders a WeasyPrint PDF must run on the dedicated "pdf"
queue, served by a worker with a small fixed concurrency (see
.platform/hooks/postdeploy/01_setup_celery.sh). A PDF task left on the
default queue would run alongside up to four others on the web instance,
which is how the payment-reminder fan-out exhausted memory in production.
"""

from django.conf import settings
from django.test import SimpleTestCase

from alumniniooerp.celery import app
from invoices import tasks as invoice_tasks


class PdfTaskQueueTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Register every app's tasks, as the worker's autodiscovery does
        app.loader.import_default_modules()

    def _queue_for(self, task_name):
        return app.amqp.router.route({}, task_name)["queue"].name

    def test_every_pdf_rendering_task_is_routed_to_the_pdf_queue(self):
        for name in settings.CELERY_PDF_TASKS:
            with self.subTest(task=name):
                self.assertIn(name, app.tasks, f"{name} is not a registered task")
                self.assertEqual(self._queue_for(name), settings.CELERY_PDF_QUEUE)

    def test_pdf_task_list_covers_the_known_renderers(self):
        expected = {
            "core.tasks.send_invoice_email_task",
            "core.tasks.send_quote_email_task",
            "core.tasks.send_delivery_order_email_task",
            "core.tasks.send_asset_transaction_email_task",
            "invoices.tasks.build_invoice_pdf_zip",
            "invoices.tasks.send_payment_reminder_email",
            "invoices.tasks.send_consolidated_payment_reminder_email",
            "configurator.tasks.render_export_template_preview",
            "core.tasks.render_document_task",
            "assets.tasks.generate_asset_transaction_document",
            "assets.tasks.generate_asset_transaction_receipt_document",
            "assets.tasks.generate_asset_transaction_pause_document",
            "invoices.tasks.attach_intercompany_debit_note_pdf",
        }
        self.assertEqual(set(settings.CELERY_PDF_TASKS), expected)

    def test_other_tasks_stay_on_the_default_queue(self):
        for name in (
            "invoices.tasks.process_payment_reminders",
            "invoices.tasks.send_payment_reminder_report",
            "core.tasks.send_client_terms_email_task",
            "banking.tasks.sync_bank_transactions",
        ):
            with self.subTest(task=name):
                self.assertEqual(self._queue_for(name), settings.CELERY_TASK_DEFAULT_QUEUE)

    def test_reminder_sends_are_rate_limited(self):
        limit = invoice_tasks.PAYMENT_REMINDER_SEND_RATE_LIMIT
        self.assertEqual(invoice_tasks.send_payment_reminder_email.rate_limit, limit)
        self.assertEqual(
            invoice_tasks.send_consolidated_payment_reminder_email.rate_limit, limit
        )
