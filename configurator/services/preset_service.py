from decimal import Decimal

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
    quantity: int = 1,
) -> "Configuration | list[Configuration]":
    """
    Single source of truth for creating a Configuration from a preset.

    Creates the Configuration(s), fills parameter values, copies defaults,
    generates BOM & recalculates price.

    If quantity > 1, creates that many identical configurations
    and returns a list. Otherwise returns a single Configuration.

    Args:
        preset: The ConfigurationPreset to use.
        quote: Optional Quote to link the configuration to.
        merged_values: Dict of parameter name → value (preset values + overrides).
                       Defaults to preset.parameter_values if not provided.
        asset: Optional Asset to link.
        client: Optional Client. If not provided, uses quote.client.
        quantity: Number of configurations to create (default 1).
    """
    if merged_values is None:
        merged_values = dict(preset.parameter_values)

    count = max(1, int(quantity))
    configs = []

    for i in range(count):
        config = _create_single_config(
            preset=preset,
            quote=quote,
            merged_values=merged_values,
            asset=asset,
            client=client,
            suffix=f" ({i+1}/{count})" if count > 1 else "",
        )
        configs.append(config)

    return configs if count > 1 else configs[0]


def _create_single_config(
    preset, quote, merged_values, asset, client, suffix="",
) -> Configuration:
    """Create one Configuration from a preset."""
    product = preset.product
    if client is None and quote:
        client = quote.client

    # 1. Create Configuration
    name = f"{preset.name}{suffix}" if suffix else preset.name
    config = Configuration.objects.create(
        name=name,
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

    # 5. Apply final price override if set
    if preset.final_price and preset.final_price.amount > 0:
        config.price_override = preset.final_price
        config.save(update_fields=["price_override", "price_override_currency"])
        config.recalculate_price()

    return config
