import logging

from django.conf import settings
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from configurator.services.export.ai import GeminiTemplateGenerator
from configurator.services.export.ai_service import ExportTemplateAIService

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name="dispatch")
class ExportTemplateAIGenerateView(PermissionRequiredMixin, View):
    permission_required = "configurator.change_configurationexporttemplate"

    def post(self, request, *args, **kwargs):
        api_key = settings.GEMINI_API_KEY
        if not api_key:
            logger.error("GEMINI_API_KEY not configured.")
            return JsonResponse({"error": "AI generation is not configured."}, status=503)

        is_generic = request.POST.get("generic") == "1"
        prompt = request.POST.get("prompt", "").strip()
        if not is_generic and not prompt:
            return JsonResponse({"error": "A prompt is required."}, status=400)

        kind = request.POST.get("kind", "quotation")
        product_id = request.POST.get("product") or None
        company_id = request.POST.get("company") or None
        object_id = request.POST.get("object_id") or None
        current_html = request.POST.get("html", "")
        current_css = request.POST.get("css", "")

        service = ExportTemplateAIService(GeminiTemplateGenerator(api_key=api_key))

        try:
            if is_generic:
                result = service.generate_generic(
                    kind=kind,
                    product_id=product_id,
                    company_id=company_id,
                    object_id=object_id,
                )
            else:
                result = service.generate(
                    kind=kind,
                    product_id=product_id,
                    company_id=company_id,
                    object_id=object_id,
                    current_html=current_html,
                    current_css=current_css,
                    prompt=prompt,
                )
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)
        except Exception:
            logger.exception("AI template generation failed.")
            return JsonResponse({"error": "AI generation failed. Please try again."}, status=502)

        return JsonResponse({"html": result.html, "css": result.css})
