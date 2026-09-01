"""
Orchestrates AI-assisted export template generation. Provider-agnostic - depends on
TemplateAIInterface, not on Gemini specifically (see ai/gemini_generator.py for the
concrete adapter actually wired in by the view).
"""

from .ai import TemplateAIInterface, TemplateGenerationResult
from .kind_registry import get_preview_object
from .service import ConfigurationExportService
from .skeleton import DEFAULT_SKELETON_HTML, DEFAULT_SKELETON_CSS

DEFAULT_GENERIC_PROMPT = (
    "No specific instruction was given. Generate a clean, professional, standard layout "
    "for this document kind using the available context variables - fill in appropriate "
    "sections (e.g. line items, totals, relevant fields) based on what data is available."
)


class ExportTemplateAIService:

    def __init__(self, generator: TemplateAIInterface):
        self.generator = generator

    def generate(
        self,
        *,
        kind: str,
        product_id=None,
        company_id=None,
        object_id=None,
        current_html: str = "",
        current_css: str = "",
        prompt: str,
    ) -> TemplateGenerationResult:
        target_obj = get_preview_object(
            kind, product_id=product_id, company_id=company_id, object_id=object_id
        )
        if not target_obj:
            raise ValueError(f"No object available to build context for kind '{kind}'.")

        context_json = ConfigurationExportService.get_serialized_context(target_obj)

        previous_html = current_html or DEFAULT_SKELETON_HTML
        previous_css = current_css or DEFAULT_SKELETON_CSS

        return self.generator.generate(
            prompt=prompt,
            kind=kind,
            context_json=context_json,
            previous_html=previous_html,
            previous_css=previous_css,
        )

    def generate_generic(self, *, kind, product_id=None, company_id=None, object_id=None):
        """
        Generic/no-prompt path: always starts from the skeleton (ignores any existing
        html/css, even if the template already has content) and uses a canned instruction
        instead of a user prompt.
        """
        return self.generate(
            kind=kind,
            product_id=product_id,
            company_id=company_id,
            object_id=object_id,
            current_html="",
            current_css="",
            prompt=DEFAULT_GENERIC_PROMPT,
        )
