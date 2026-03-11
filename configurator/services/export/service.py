from typing import TYPE_CHECKING, Any, Dict
from django.template import engines
from django.template.loader import get_template
from django.db.models import Q, QuerySet, Model
from django.core.serializers.json import DjangoJSONEncoder
from decimal import Decimal
from core.admin.company_access import get_user_companies
import json
import base64

from .helpers import strip_wrappers, strip_css_wrappers, clean_final_html
from .base import BaseExportStrategy
from .configuration import ConfigurationExportStrategy
from .production_item import ProductionItemExportStrategy
from .sales_invoice import SalesInvoiceExportStrategy

if TYPE_CHECKING:
    from configurator.models import ConfigurationExportTemplate

class CustomJSONEncoder(DjangoJSONEncoder):
    """
    Custom JSON encoder that handles Django models and QuerySets.
    """
    def default(self, obj):
        if isinstance(obj, Model):
            from django.forms.models import model_to_dict
            try:
                # Get basic dict
                data = model_to_dict(obj)
                
                # Model to dict doesn't always handle everything (like properties or MoneyFields)
                # Let's add the __str__ as a 'label' for convenience
                data['__str__'] = str(obj)
                
                # Cleanup data for JSON
                for key, value in list(data.items()):
                    if isinstance(value, Decimal):
                        data[key] = float(value)
                    elif hasattr(value, 'amount'): # djmoney Money object
                        data[key] = float(value.amount)
                
                return data
            except Exception:
                return str(obj)
        
        if isinstance(obj, QuerySet):
            return list(obj)
        
        if isinstance(obj, Decimal):
            return float(obj)
            
        if hasattr(obj, 'amount') and hasattr(obj, 'currency'):
            return float(obj.amount)

        return super().default(obj)

class ConfigurationExportService:
    """
    Service for resolving and rendering export templates for configurations.
    """

    @staticmethod
    def get_strategy(obj: Any) -> BaseExportStrategy:
        from configurator.models import Configuration
        from production.models import ProductionItem
        from invoices.models import SalesInvoice

        if isinstance(obj, Configuration):
            return ConfigurationExportStrategy(obj)
        elif isinstance(obj, ProductionItem):
            return ProductionItemExportStrategy(obj)
        
        if isinstance(obj, SalesInvoice):
            return SalesInvoiceExportStrategy(obj)
        
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
    def get_serialized_context(obj: Any) -> str:
        """
        Returns the context for the object as a JSON string.
        """
        strategy = ConfigurationExportService.get_strategy(obj)
        context = strategy.get_context()
        return json.dumps(context, cls=CustomJSONEncoder)

    @staticmethod
    def render_parts(obj: Any, template: "ConfigurationExportTemplate"):
        """
        Renders the template and returns the rendered body, clean CSS, and serialized context.
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

        serialized_context = json.dumps(context, cls=CustomJSONEncoder)

        return rendered_body, clean_css, serialized_context

    @staticmethod
    def render_html(obj: Any, template: "ConfigurationExportTemplate"):
        rendered_body, clean_css, serialized_context = ConfigurationExportService.render_parts(obj, template)

        wrapper_tpl = get_template("exports/base_export.html")

        return wrapper_tpl.render({
            "body": rendered_body,
            "css": clean_css,
            "context_json": serialized_context,
        })
