from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _

from assets.models import AssetTransaction
from configurator.models import ConfigurationExportTemplate, ExportTemplateKind
from configurator.services.export.service import ConfigurationExportService
from configurator.templatetags.formatting import qr


def asset_transaction_document_view(request, token):
    transaction = get_object_or_404(
        AssetTransaction.objects.select_related(
            "asset", "asset__asset_type", "asset__client",
            "company", "project_file", "project_file__client",
            "from_zone", "to_zone", "performed_by",
        ),
        public_token=token,
    )
    template = ConfigurationExportTemplate.get_default_for_product(
        product=None,
        kind=ExportTemplateKind.ASSET_TRANSACTION,
        company=transaction.company,
    )
    if template is None:
        raise Http404(_("Asset transaction document is not available."))
    document_url = request.build_absolute_uri(request.path)
    html = ConfigurationExportService.render_html(
        transaction,
        template,
        extra_context={
            "document_url": document_url,
            "document_qr": qr(document_url),
        },
    )
    return HttpResponse(html, content_type="text/html; charset=utf-8")
