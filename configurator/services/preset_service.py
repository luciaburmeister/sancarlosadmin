from configurator.models import (
    Configuration,
    ConfigurationParameterValue,
    ConfigurationStatus,
    ConfigurationPreset,
)
from sales.models import Quote


def create_configuration_from_preset(
    preset: ConfigurationPreset,
    quote: Quote = None,
    merged_values: dict = None,
    asset=None,
    client=None,
) -> Configuration:
    """
    Single source of truth for creating a Configuration from a preset.

    Creates the Configuration, fills parameter values, copies defaults,
    generates BOM & recalculates price.

    Args:
        preset: The ConfigurationPreset to use.
        quote: Optional Quote to link the configuration to.
        merged_values: Dict of parameter name → value (preset values + overrides).
                       Defaults to preset.parameter_values if not provided.
        asset: Optional Asset to link.
        client: Optional Client. If not provided, uses quote.client.
    """
    if merged_values is None:
        merged_values = dict(preset.parameter_values)

    product = preset.product
    if client is None and quote:
        client = quote.client

    # 1. Create Configuration
    config = Configuration.objects.create(
        name=preset.name,
        product=product,
        status=ConfigurationStatus.DRAFT,
        client=client,
        asset=asset,
        company=product.company,
        quote=quote,
    )

    # 2. Create ConfigurationParameterValue records
    for param in product.parameters.all():
        raw_value = merged_values.get(param.name)
        if raw_value is None:
            raw_value = param.default_value or ""

        ConfigurationParameterValue.objects.create(
            configuration=config,
            parameter=param,
            value=str(raw_value),
            order=param.order,
        )

    # 3. Setup defaults from product
    config.copy_default_pricing_rules_from_product()
    config.copy_default_taxes_from_product()
    config.copy_taxes_from_client()

    # 4. Generate BOM and recalculate price
    config.generate_bom()
    config.recalculate_price()

    return config
