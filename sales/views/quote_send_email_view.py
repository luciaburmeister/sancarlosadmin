"""
Quote Send Email view — recipient picker with CC support.

Thin subclass of the reusable BaseSendDocumentEmailView (core), which
provides the recipient picker (client email + contacts) and CCs the sender.
"""

from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from configurator.models import ConfigurationExportTemplate, ExportTemplateKind
from configurator.services.export import ConfigurationExportService
from core.tasks import send_quote_email_task
from core.views.send_document_email_view import BaseSendDocumentEmailView
from sales.models import Quote


class QuoteSendEmailView(BaseSendDocumentEmailView):
    title = _("Send Quote Email")
    permission_required = ("sales.change_quote",)
    model = Quote
    pk_url_kwarg = "quote_id"

    def get_available_templates(self, obj):
        # Scoped directly to the quote's own company rather than reusing
        # ConfigurationExportService.get_available_templates(), which narrows
        # by the admin's currently active company in the switcher - that would
        # hide this quote's own templates whenever the two don't match.
        product = ConfigurationExportService.get_strategy(obj).get_product()
        return ConfigurationExportTemplate.objects.filter(
            Q(product=product) | Q(product__isnull=True),
            Q(company=obj.company) | Q(company__isnull=True),
            kind=ExportTemplateKind.CONSOLIDATED_QUOTATION,
            is_active=True,
        )

    def get_default_template(self, obj):
        product = ConfigurationExportService.get_strategy(obj).get_product()
        return ConfigurationExportTemplate.get_default_for_product(
            product=product, kind=ExportTemplateKind.CONSOLIDATED_QUOTATION, company=obj.company,
        )

    def queue_email(self, obj, to_emails, cc_emails, reply_to, template_id=None):
        send_quote_email_task.delay(
            obj.id,
            to_emails=to_emails,
            cc_emails=cc_emails,
            reply_to=reply_to,
            template_id=template_id,
        )
