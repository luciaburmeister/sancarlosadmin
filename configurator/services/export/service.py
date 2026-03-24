from typing import TYPE_CHECKING, Any
from django.template import engines
from django.template.loader import get_template
from django.db.models import Q, QuerySet, Model
from django.core.serializers.json import DjangoJSONEncoder
from decimal import Decimal
from core.admin.company_access import get_user_companies
import json
from django.template import engines
from configurator.utils import safe_eval
from .helpers import strip_wrappers, strip_css_wrappers, clean_final_html
from .base import BaseExportStrategy
from .configuration import ConfigurationExportStrategy
from .production_item import ProductionItemExportStrategy
from .sales_invoice import SalesInvoiceExportStrategy
import re
from django.db.models.fields.files import FieldFile

if TYPE_CHECKING:
    from configurator.models import ConfigurationExportTemplate, Configuration


class CustomJSONEncoder(DjangoJSONEncoder):
    """
    Custom JSON encoder that handles Django models and QuerySets.
    """
    def default(self, obj):
        if isinstance(obj, FieldFile):
            return None

        if isinstance(obj, Model):
            from django.forms.models import model_to_dict
            try:
                # Get basic dict
                data = model_to_dict(obj)
                
                # Model to dict doesn't always handle everything (like properties or MoneyFields)
                # Let's add the __str__ as a 'label' for convenience
                data['__str__'] = str(obj)
                
                # Cleanup data for JSON
                keys_to_delete = []
                for key, value in list(data.items()):
                    if isinstance(value, Decimal):
                        data[key] = float(value)
                    elif hasattr(value, 'amount'): # djmoney Money object
                        data[key] = float(value.amount)
                    elif isinstance(value, FieldFile):
                        keys_to_delete.append(key)

                for key in keys_to_delete:
                    del data[key]
                
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
        json_str = json.dumps(context, cls=CustomJSONEncoder)
        return json_str.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")

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
        serialized_context = serialized_context.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")

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

    @staticmethod
    def evaluate_configuration_templates(configuration):
        """
        Evaluates ProductComputedTemplates for a configuration using its export context.
        """
        

        strategy = ConfigurationExportService.get_strategy(configuration)
        
        try:
            # We must get context without evaluating rendered_templates to avoid circular evaluation
            base_context = strategy.get_context(include_rendered_templates=False)
        except TypeError:
            # Fallback if strategy doesn't support the flag natively
            base_context = strategy.get_context()

        rendered_templates = {}
        if getattr(configuration, "product", None):
            django_engine = engines["django"]
            computed_templates = configuration.product.computed_templates.all().order_by("name", "order")
            resolved_names = set()

            for tmpl in computed_templates:
                if tmpl.name in resolved_names:
                    continue
                    
                is_match = True
                expr = (tmpl.condition_expression or "").strip()
                if expr:
                    try:
                        # Use base_context for conditions so they can reference standard config variables
                        is_match = bool(safe_eval(expr, base_context))
                    except Exception:
                        is_match = False
                        
                if is_match:
                    try:
                        user_tpl = django_engine.from_string(tmpl.template)
                        rendered_html = user_tpl.render(base_context)
                    except Exception as e:
                        rendered_html = f"<!-- Template Error in {tmpl.name}: {e} -->"
                        
                    # Clean up white space and new lines before storing
                    rendered_html = re.sub(r'[\r\n]+', ' ', rendered_html)
                    rendered_html = re.sub(r'\s{2,}', ' ', rendered_html).strip()
                    
                    rendered_templates[tmpl.name] = rendered_html
                    resolved_names.add(tmpl.name)

        return rendered_templates
