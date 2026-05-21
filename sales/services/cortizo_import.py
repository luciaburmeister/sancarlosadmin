import json
import logging
import re
from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from company.models import Company
from sales.models import Client, Quote, Order
from materials.models import Material, Component, Category, Provider, UnitChoices
import base64
from django.core.files.base import ContentFile
from configurator.models import (
    ProductTemplate, Configuration, ConfigurationItem, 
    ConfigurationParameterValue, ProductParameter, BOMRule,
    ProductTemplateComponentGroup
)
from collections import defaultdict

logger = logging.getLogger(__name__)

class CortizoImportService:
    def __init__(self, company_name="Aluares"):
        self.company = Company.objects.filter(name=company_name).first()
        if not self.company:
             self.company = Company.objects.first()
        
        self.provider, _ = Provider.objects.get_or_create(
            name="Cortizo",
            company=self.company,
            defaults={"tax_identifier": ""}
        )

    @transaction.atomic
    def import_documents(self, data):
        """
        data: List of document dictionaries from JSON.
        """
        results = []
        for doc_data in data:
            quote = self._process_document(doc_data)
            if quote is not None:
                results.append(quote)
        return results

    def _get_or_create_client(self, client_data):
        nif = client_data.get("nif", "").strip()
        client = None
        if nif:
            client = Client.objects.filter(tax_identifier=nif, company=self.company).first()
        
        if not client:
            client = Client.objects.create(
                company=self.company,
                name=client_data.get("name", "Unknown Client").strip(),
                tax_identifier=nif,
                phone=client_data.get("phone", ""),
                email=client_data.get("email", ""),
                address=f"{client_data.get('address', '')}, {client_data.get('postal_code', '')} {client_data.get('city', '')}"
            )
        return client

    def _process_document(self, doc_data):
        client_data = doc_data.get("client", {})
        nif = client_data.get("nif", "").strip()
        if not nif:
            doc_number = doc_data.get("document_number", doc_data.get("number", "unknown"))
            logger.warning(
                "Skipping Cortizo document %s: client has no tax identifier (NIF).",
                doc_number,
            )
            return None
        client = self._get_or_create_client(client_data)
        
        doc_number = doc_data.get("document_number", doc_data.get("number", ""))
        version = doc_data.get("version", "")
        project_name = f"Cortizo - {doc_number}"
        if version:
            project_name += f" - {version}"
        
        quote, created = Quote.objects.get_or_create(
            company=self.company,
            client=client,
            project_name=project_name,
            defaults={
                "status": "draft",
            }
        )
        
        if not created:
            return quote
        
        # Store IVA percent to apply to items
        iva_percent = doc_data.get("iva_amount")

        for item_data in doc_data.get("items", []):
            self._process_item(quote, item_data, iva_percent)
            
        quote.recalc_totals()
        return quote

    def _apply_tax(self, obj, tax_name, percentage):
        from invoices.models import TaxType, AppliedTax
        from django.contrib.contenttypes.models import ContentType
        
        tax_name = TaxType.normalize_tax_name(tax_name)
        tax_type, _ = TaxType.objects.get_or_create(
            tax_name=tax_name or "IVA",
            country="ES",
            defaults={
                "behavior": "add",
                "percentage_options": [4, 10, 21],
                "default_percentage": 21
            }
        )
        
        ct = ContentType.objects.get_for_model(obj)
        AppliedTax.objects.update_or_create(
            content_type=ct,
            object_id=obj.pk,
            tax_type=tax_type,
            defaults={
                "selected_percentage": Decimal(str(percentage)),
                "order": 1
            }
        )
        # Note: We don't call recalc_totals here because we handle it in _process_item or _process_document

    def _process_item(self, quote, item_data, iva_percent=None):
        description = item_data.get("description", "Unnamed Item")
        
        # Find or create ProductTemplate
        template, template_created = ProductTemplate.objects.get_or_create(
            company=self.company,
            name=description,
            defaults={
                "show_in_configurator": False,
            }
        )
        if not template_created and template.show_in_configurator:
            template.show_in_configurator = False
            template.save(update_fields=["show_in_configurator"])
        
        config = Configuration.objects.create(
            company=self.company,
            client=quote.client,
            quote=quote,
            product=template,
            name=f"{description} ({item_data.get('reference', '')})",
            status="completed"
        )
        
        # Parse dimensions
        dimensions_str = item_data.get("dimensions", "")
        dims = self._parse_dimensions(dimensions_str)
        for name, value in dims.items():
            param, _ = ProductParameter.objects.get_or_create(
                product=template,
                name=name,
                defaults={"label": name, "type": "number"}
            )
            ConfigurationParameterValue.objects.create( 
                configuration=config,
                parameter=param,
                value=str(value)
            )
            
        # Process components (BOM)
        config_total_cost = Decimal("0")
        all_item_stages = []
        stage_to_components = defaultdict(list)

        for comp_data in item_data.get("components", []):
            cost, component, comp_stages = self._process_component(config, comp_data)
            config_total_cost += cost
            for stage in comp_stages:
                if stage not in all_item_stages:
                    all_item_stages.append(stage)
                stage_to_components[stage].append(component)

        # Update ProductTemplate production stages if they changed
        if all_item_stages:
            current_stages = list(template.production_stages or [])
            changed = False
            for stage in all_item_stages:
                if stage not in current_stages:
                    current_stages.append(stage)
                    changed = True
            
            if changed:
                template.production_stages = current_stages
                template.save(update_fields=["production_stages"])

        # Group components by stage
        for stage in all_item_stages:
            components = stage_to_components[stage]
            group, _ = ProductTemplateComponentGroup.objects.get_or_create(
                product_template=template,
                stage=stage
            )
            group.components.add(*components)
            
        # Set final prices directly from JSON
        total_amount = Decimal(str(item_data.get("total_amount", 0)))
        config.subtotal_price = total_amount
        config.total_cost = config_total_cost
        
        # Apply tax to configuration if provided
        if iva_percent:
            self._apply_tax(config, "IVA", iva_percent)
        
        # Calculate final total and breakdown
        final_total, tax_breakdown = config.calculate_taxes_and_snapshot(total_amount)
        config.total_price = final_total
        config.tax_breakdown = tax_breakdown
        
        # Save Work Order PDF if present in base64
        base64_pdf = item_data.get("pdf_base64")
        if base64_pdf:
            try:
                pdf_content = base64.b64decode(base64_pdf)
                filename = f"work_order_{config.id}.pdf"
                config.work_order_pdf.save(filename, ContentFile(pdf_content), save=False)
            except Exception as e:
                # Log or handle error
                print(f"Error decoding base64 PDF for configuration {config.id}: {e}")

        config.save(update_fields=["subtotal_price", "total_price", "tax_breakdown", "total_cost", "work_order_pdf"])

    def _process_component(self, config, comp_data):
        ref = comp_data.get("reference", "UNKN").strip()
        cat_name = comp_data.get("category", "General").strip()
        production_stages = comp_data.get("production_stages", [])

        # Sync production stages with company
        if production_stages:
            company_stages = self.company.production_stages or []
            added_any = False
            for stage in production_stages:
                if stage not in company_stages:
                    company_stages.append(stage)
                    added_any = True
            if added_any:
                self.company.production_stages = company_stages
                self.company.save(update_fields=["production_stages"])
        
        category, _ = Category.objects.get_or_create(
            company=self.company,
            name=cat_name
        )
        
        # ConfigurationItem dims
        qty = Decimal(str(comp_data.get("quantity", 1)))
        measure = str(comp_data.get("measure", ""))
        
        # Robust measure parsing
        length = self._parse_measure(measure)
        
        total_cost = Decimal(str(comp_data.get("total_cost", 0)))
        
        # Calculate unit_price from total_cost for maximum precision
        # line_total = unit_price * quantity * length
        unit_price = Decimal("0")
        if qty > 0 and length > 0:
            unit_price = total_cost / (qty * length)
        elif qty > 0:
            unit_price = total_cost / qty

        # Material
        material, _ = Material.objects.get_or_create(
            company=self.company,
            reference_code=ref,
            defaults={
                "name": comp_data.get("description", ref),
                "category": category,
                "provider": self.provider,
                "cost_per_unit": unit_price,
            }
        )

        # Component
        component, created = Component.objects.get_or_create(
            company=self.company,
            code=ref,
            defaults={
                "name": comp_data.get("description", ref),
                "unit_price_expression": "base_cost",
                "production_stages": production_stages,
                "cost_per_unit": unit_price,
            }
        )
        if not created:
            # Update production stages if they represent a new subset
            if production_stages:
                current_stages = list(component.production_stages or [])
                changed = False
                for stage in production_stages:
                    if stage not in current_stages:
                        current_stages.append(stage)
                        changed = True
                
                if changed:
                    component.production_stages = current_stages
                    component.save(update_fields=["production_stages"])
            
            # Always update cost to latest from Cortizo
            component.cost_per_unit = unit_price
            component.save(update_fields=["cost_per_unit"])

        if not component.materials.exists():
            component.materials.add(material)

        # Ensure Material cost is consistent
        material.cost_per_unit = unit_price
        material.save(update_fields=["cost_per_unit"])
        
        ConfigurationItem.objects.create(
            configuration=config,
            component=component,
            quantity=qty,
            length=length,
            unit_price=unit_price,
            is_template_item=True,
            production_stages=production_stages,
            note=measure
        )

        # Create BOMRule with correct stages and fixed values (quantity=1, length=1)
        BOMRule.objects.get_or_create(
            product=config.product,
            component=component,
            quantity_expression="1",
            length_expression="1",
            production_stages=production_stages,
            defaults={
                "company": self.company,
                "description": comp_data.get("description", ""),
            }
        )

        return total_cost, component, production_stages

    def _parse_measure(self, measure_str):
        if not measure_str:
            return Decimal("1")
        
        # Handle "WxH" formats (e.g. glass)
        if "x" in measure_str.lower():
            return Decimal("1")
            
        try:
            # Clean numeric string: "1.234,56" -> "1234.56"
            cleaned = measure_str.replace(".", "").replace(",", ".")
            val = Decimal(cleaned)
            return val if val > 0 else Decimal("1")
        except:
            return Decimal("1")

    def _parse_dimensions(self, dim_str):
        """ Parses L=750;A=2.300; into {'L': 750, 'A': 2.3} """
        dims = {}
        if not dim_str:
            return dims
        parts = dim_str.split(";")
        for p in parts:
            if "=" in p:
                k, v = p.split("=")
                try:
                    # Clean numeric values
                    val_str = v.replace(".", "").replace(",", ".")
                    dims[k.strip()] = float(val_str)
                except:
                    pass
        return dims
