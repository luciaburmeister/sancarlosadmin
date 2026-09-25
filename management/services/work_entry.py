import logging
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

logger = logging.getLogger(__name__)


class _LazyComponentAccessor:
    """
    Lazy accessor for component pricing. When a pricing step expression
    references `components.tarifa_cuba`, this evaluates the component's
    price rules using the *current* step context, so variables set by
    earlier steps (e.g. MATERIAL_CUBA, CAPACIDAD) are available to the
    component's price rule conditions.
    """

    def __init__(self, components_qs):
        self._components = {}
        for comp in components_qs:
            slug = comp.name.lower().replace(" ", "_")
            self._components[slug] = comp
        self._context = {}

    def bind_context(self, context: dict):
        """Update the context that will be passed to compute_unit_price."""
        self._context = context

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        comp = self._components.get(name)
        if comp is None:
            raise AttributeError(f"Component '{name}' not linked to this WorkEntryType")
        computed_price = comp.compute_unit_price(context=dict(self._context))
        return float(computed_price.amount)

    def __repr__(self):
        return f"<LazyComponents: {list(self._components.keys())}>"


class WorkEntryService:
    """
    Service for building expression contexts and managing work entry
    processing status for source objects.
    """

    # Registry: model_label → callable(instance) → dict
    _context_builders: dict[str, callable] = {}

    @classmethod
    def register_context_builder(cls, model_class, builder_fn):
        """
        Register a custom context builder for a model class.
        The builder_fn receives the source instance and returns a dict.
        """
        label = model_class._meta.label_lower
        cls._context_builders[label] = builder_fn

    @classmethod
    def build_source_context(cls, source_object) -> dict:
        """
        Build the expression context from a source object.

        Layers:
        1. Custom builder (if registered for this model)
        2. Fallback: extract model fields + .data JSONField
        """
        if source_object is None:
            return {}

        label = source_object._meta.label_lower

        # Check for a registered custom builder
        if label in cls._context_builders:
            return cls._context_builders[label](source_object)

        # Fallback: auto-extract fields
        return cls._auto_extract_context(source_object)

    @classmethod
    def _auto_extract_context(cls, obj) -> dict:
        """
        Auto-extract context from a model instance:
        - All concrete field values (excluding PKs and FKs)
        - FK display values (e.g. from_zone → from_zone name)
        - .data JSONField contents merged into context
        """
        context = {}

        for field in obj._meta.get_fields():
            if not hasattr(field, "attname"):
                continue

            name = field.name

            # Skip internal fields (but keep id/pk for description templates)
            if name in ("company", "company_id"):
                continue

            try:
                value = getattr(obj, name, None)
            except Exception:
                continue

            if value is None:
                continue

            # For ForeignKey fields, use the string representation
            if hasattr(field, "related_model") and field.related_model:
                context[name] = str(value)
            elif hasattr(value, "amount"):
                # MoneyField — extract the numeric amount
                context[name] = float(value.amount)
            else:
                context[name] = value

        data = getattr(obj, "data", None)
        if isinstance(data, dict):
            from core.text import normalize_match_data

            context["data"] = normalize_match_data(data)

        return context

    @classmethod
    def build_full_context(cls, entry_type, source_object=None, quantity=Decimal("1.00")) -> dict:
        """
        Build the complete context for pricing evaluation:
        1. Base: quantity
        2. Source fields
        3. Linked materials (static cost_per_unit)
        4. Linked components (dynamic: evaluated via price rules with context)
        """
        context = {
            "quantity": float(quantity),
        }

        # Layer 2: Source context
        if source_object is not None:
            source_ctx = cls.build_source_context(source_object)
            context.update(source_ctx)

        # Layer 3: Linked materials pricing (static cost)
        if entry_type.pk:
            materials_ctx = {}
            for mat in entry_type.materials.all():
                if mat.rule_key:
                    materials_ctx[mat.rule_key] = float(mat.cost_per_unit.amount)
            if materials_ctx:
                context["materials"] = materials_ctx

            # Layer 4: Linked components as lazy accessor
            # Components are NOT eagerly evaluated — they become a lazy
            # object so pricing step expressions can set variables (e.g.
            # MATERIAL_CUBA, CAPACIDAD) BEFORE the component's price rules
            # evaluate against them.
            components_qs = entry_type.components.prefetch_related("price_rules", "materials").all()
            if components_qs:
                context["components"] = _LazyComponentAccessor(components_qs)

        return context

    @classmethod
    def get_billing_status(cls, source_object):
        """
        For a given source (e.g. AssetTransaction), find all applicable
        WorkEntryTypes and check which ones have been fulfilled.

        Resolution order per category:
        1. Project-file-scoped type (if source has a project_file)
        2. Company-wide default (project_file=NULL)

        Returns:
            dict with 'expected', 'existing_type_ids', 'missing', 'is_fully_processed'
        """
        from management.models import WorkEntryType, WorkEntry, CostEntry

        ct = ContentType.objects.get_for_model(source_object)
        context = cls.build_source_context(source_object)

        # Find all WorkEntryTypes that could apply to this source
        # Include types that explicitly match this source model OR have no source restriction
        from django.db.models import Q

        applicable_types = WorkEntryType.objects.filter(
            Q(source_content_type=ct) | Q(source_content_type__isnull=True),
            is_active=True,
            company=source_object.company,
        )

        # Also include project-file-scoped types if the source has a project_file
        project_file = getattr(source_object, "project_file", None)
        if project_file:
            pf_types = WorkEntryType.objects.filter(
                Q(source_content_type=ct) | Q(source_content_type__isnull=True),
                project_file=project_file,
                is_active=True,
            )
            applicable_types = applicable_types.filter(project_file__isnull=True) | pf_types

        # Evaluate applicability and deduplicate by (category, direction).
        # Per key: project-file-scoped type wins over company-wide default.
        # A category may hold one revenue and one cost type; both are expected.
        # Types without a category pass through without deduplication.
        by_category = {}  # (category_id, direction) → WorkEntryType
        no_category = []  # types without a category (backward compat)
        for wtype in applicable_types.select_related("category"):
            if not wtype.evaluate_applicability(context):
                continue

            cat_id = wtype.category_id
            if cat_id is None:
                no_category.append(wtype)
                continue

            key = (cat_id, wtype.direction)
            existing = by_category.get(key)

            if existing is None:
                by_category[key] = wtype
            elif wtype.project_file_id and not existing.project_file_id:
                # Project-file-scoped overrides company-wide default
                by_category[key] = wtype

        expected = list(by_category.values()) + no_category

        # Check which have been created (type ids are disjoint by direction,
        # so the union across both entry tables is safe).
        source_filter = {
            "source_content_type": ct,
            "source_object_id": source_object.pk,
        }
        existing_type_ids = set(
            WorkEntry.objects.filter(**source_filter).values_list("entry_type_id", flat=True)
        ) | set(
            CostEntry.objects.filter(**source_filter).values_list("entry_type_id", flat=True)
        )

        missing = [t for t in expected if t.id not in existing_type_ids]

        return {
            "expected": expected,
            "existing_type_ids": existing_type_ids,
            "missing": missing,
            "is_fully_processed": len(expected) > 0 and len(missing) == 0,
            "has_expectations": len(expected) > 0,
            "count_expected": len(expected),
            "count_existing": len(existing_type_ids & {t.id for t in expected}),
        }

    @classmethod
    def create_entry_from_source(cls, entry_type, source_object, project_file, quantity=Decimal("1.00")):
        """
        Create a WorkEntry (revenue type) or CostEntry (cost type) from a
        source object using the entry_type's pricing steps and description
        template. Applied taxes are copied onto WorkEntries only.
        """
        from management.models import WorkEntry

        model_class = entry_type.entry_model
        context = cls.build_full_context(entry_type, source_object, quantity)

        # Compute unit price
        unit_price = entry_type.compute_unit_price(context)

        # Build description
        description = ""
        if entry_type.description_template:
            try:
                # Build a string-safe copy: flatten non-string-safe objects
                desc_ctx = {}
                for k, v in context.items():
                    if isinstance(v, dict):
                        # Flatten nested dicts: data.tipo_cuba → tipo_cuba
                        for dk, dv in v.items():
                            desc_ctx[dk] = dv
                    elif isinstance(v, (str, int, float, Decimal)):
                        desc_ctx[k] = v
                    else:
                        desc_ctx[k] = str(v)
                description = entry_type.description_template.format(**desc_ctx)
            except Exception:
                description = entry_type.name
        if not description:
            description = entry_type.name

        ct = ContentType.objects.get_for_model(source_object)

        entry = model_class(
            project_file=project_file,
            entry_type=entry_type,
            description=description,
            quantity=quantity,
            unit_price=unit_price,
            date=getattr(source_object, "date", None) or timezone.now().date(),
            source_content_type=ct,
            source_object_id=source_object.pk,
            company=project_file.company,
        )
        entry.full_clean(exclude=["description"])
        entry.save()

        if model_class is not WorkEntry:
            return entry

        # Copy AppliedTax from the WorkEntryType to the new WorkEntry
        from invoices.models import AppliedTax
        entry_ct = ContentType.objects.get_for_model(WorkEntry)
        for tax in AppliedTax.objects.filter(
            content_type=ContentType.objects.get_for_model(entry_type),
            object_id=entry_type.pk,
        ):
            base = entry.total_price.amount
            calculated = base * tax.selected_percentage / Decimal("100")
            AppliedTax.objects.create(
                tax_type=tax.tax_type,
                selected_percentage=tax.selected_percentage,
                base_amount=base,
                calculated_amount=calculated,
                content_type=entry_ct,
                object_id=entry.pk,
                order=tax.order,
            )

        return entry
