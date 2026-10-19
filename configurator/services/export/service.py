from typing import TYPE_CHECKING, Any
from django.template import engines
from django.template.loader import get_template
from django.db.models import Q, QuerySet, Model
from django.core.serializers.json import DjangoJSONEncoder
from django.utils import translation
from decimal import Decimal
from core.admin.company_access import get_user_companies
import json
import logging
from django.template import engines
from configurator.utils import safe_eval
from .helpers import strip_wrappers, strip_css_wrappers, clean_final_html
from .base import BaseExportStrategy
import re
from django.db.models.fields.files import FieldFile
from django_countries.fields import Country

if TYPE_CHECKING:
    from configurator.models import ConfigurationExportTemplate, Configuration

logger = logging.getLogger(__name__)


class CustomJSONEncoder(DjangoJSONEncoder):
    """
    Custom JSON encoder that handles Django models and QuerySets.
    """
    def default(self, obj):
        if isinstance(obj, FieldFile):
            return None
            
        try:
            if isinstance(obj, Country):
                return {
                    "code": obj.code,
                    "name": str(obj.name)
                }
        except ImportError:
            pass

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
                    elif isinstance(value, list):
                        # many-to-many: model_to_dict gives instances; expanding
                        # them loops on cycles (Asset.excluded_assets is symmetrical)
                        data[key] = [item.pk if isinstance(item, Model) else item for item in value]

                for key in keys_to_delete:
                    del data[key]

                return data
            except RecursionError:
                # Relations too deep to walk -- this is debug/AI-assist JSON
                # only, so fall back to a placeholder instead of failing.
                return f"<{obj.__class__.__name__} pk={getattr(obj, 'pk', None)}>"
            except Exception:
                try:
                    return str(obj)
                except RecursionError:
                    return f"<{obj.__class__.__name__} pk={getattr(obj, 'pk', None)}>"
        
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
        from .strategy_registry import get_strategy_map

        for model_cls, strategy_cls in get_strategy_map().items():
            if isinstance(obj, model_cls):
                return strategy_cls(obj)

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
        return ConfigurationExportService._safe_serialize(context)

    @staticmethod
    def _safe_serialize(context: dict) -> str:
        """json.dumps(context, cls=CustomJSONEncoder), never raising -- this
        output is debug/AI-assist only (context_json in wrap_parts), so a
        serialization failure must never block the actual document render."""
        try:
            json_str = json.dumps(context, cls=CustomJSONEncoder)
        except Exception:
            logger.warning("Failed to serialize export context for debug/AI use", exc_info=True)
            return "{}"
        return json_str.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")

    @staticmethod
    def document_language(obj: Any) -> str:
        """The language ``obj``'s documents print in: its client's, else Spanish."""
        from company.models import DocumentLanguage

        client = getattr(obj, "client", None)
        return getattr(client, "language", "") or DocumentLanguage.ES

    @staticmethod
    def render_parts(obj: Any, template: "ConfigurationExportTemplate", extra_context=None):
        """
        Renders the template and returns the rendered body, clean CSS, and serialized context.

        It renders in the template's language, else the document's
        (document_language): dates, numbers and the labels the context
        carries (payment method, document type…) follow it.
        """
        # Clean HTML & CSS first
        clean_html = strip_wrappers(template.html or "")
        clean_css = strip_css_wrappers(template.css or "")

        language = template.language or ConfigurationExportService.document_language(obj)
        with translation.override(language):
            strategy = ConfigurationExportService.get_strategy(obj)
            context = strategy.get_context()
            if extra_context:
                context.update(extra_context)

            django_engine = engines["django"]

            user_tpl = django_engine.from_string(clean_html)
            rendered_body = user_tpl.render(context)
        rendered_body = clean_final_html(rendered_body)

        serialized_context = ConfigurationExportService._safe_serialize(context)

        return rendered_body, clean_css, serialized_context

    @staticmethod
    def render_html(obj: Any, template: "ConfigurationExportTemplate", extra_context=None):
        rendered_body, clean_css, serialized_context = ConfigurationExportService.render_parts(obj, template, extra_context=extra_context)
        return ConfigurationExportService.wrap_parts(rendered_body, clean_css, serialized_context)

    @staticmethod
    def wrap_parts(rendered_body: str, clean_css: str, serialized_context: str = "") -> str:
        """
        Wraps already-rendered body/CSS in the shared export page (print base
        stylesheet, fonts, page setup). Kept separate from render_html so the
        preview can render the parts once and reuse them.
        """
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
