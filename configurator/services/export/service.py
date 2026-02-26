from typing import TYPE_CHECKING, Any
from django.template import engines
from django.template.loader import get_template
from django.db.models import Q
from core.admin.company_access import get_user_companies

from .helpers import strip_wrappers, strip_css_wrappers, clean_final_html
from .base import BaseExportStrategy
from .configuration import ConfigurationExportStrategy
from .production_item import ProductionItemExportStrategy

if TYPE_CHECKING:
    from configurator.models import ConfigurationExportTemplate

class ConfigurationExportService:
    """
    Service for resolving and rendering export templates for configurations.
    """

    @staticmethod
    def get_strategy(obj: Any) -> BaseExportStrategy:
        from configurator.models import Configuration
        from production.models import ProductionItem

        if isinstance(obj, Configuration):
            return ConfigurationExportStrategy(obj)
        elif isinstance(obj, ProductionItem):
            return ProductionItemExportStrategy(obj)
        
        raise ValueError(f"No export strategy found for object type: {type(obj)}")

    @staticmethod
    def get_available_templates(obj: Any, user, kind: str | None = None):
        """
        Returns all active export templates that apply to this object.
        """
        from configurator.models import ConfigurationExportTemplate
        
        strategy = ConfigurationExportService.get_strategy(obj)
        product = strategy.get_product()
        user_companies = get_user_companies(user)

        filters = (Q(product=product) | Q(product__isnull=True)) \
                  & (Q(company__in=user_companies) | Q(company__isnull=True)) \
                  & Q(is_active=True)

        if kind:
            filters &= Q(kind=kind)

        return (
            ConfigurationExportTemplate.objects.filter(filters)
            .select_related("product")
        )

    @staticmethod
    def render_parts(obj: Any, template: "ConfigurationExportTemplate"):
        """
        Renders the template and returns the rendered body and clean CSS as a tuple.
        """
        # Clean HTML & CSS first
        clean_html = strip_wrappers(template.html or "")
        clean_css = strip_css_wrappers(template.css or "")

        strategy = ConfigurationExportService.get_strategy(obj)
        context = strategy.get_context()

        django_engine = engines["django"]

        user_tpl = django_engine.from_string(clean_html)
        rendered_body = user_tpl.render(context)
        rendered_body = clean_final_html(rendered_body)

        return rendered_body, clean_css

    @staticmethod
    def render_html(obj: Any, template: "ConfigurationExportTemplate"):
        rendered_body, clean_css = ConfigurationExportService.render_parts(obj, template)

        wrapper_tpl = get_template("exports/base_export.html")

        return wrapper_tpl.render({
            "body": rendered_body,
            "css": clean_css,
        })
