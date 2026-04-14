from configurator.models import Configuration, ConfigurationItem
from configurator.resources.configuration_resource import ConfigurationItemResource

# Let's see what happens if we pass a Configuration queryset to ConfigurationItemResource.export()
# if we override the export method.

class EnhancedConfigurationItemResource(ConfigurationItemResource):
    def export(self, queryset=None, *args, **kwargs):
        if queryset is not None and queryset.model == Configuration:
            print(f"Expanding Configuration queryset of size {queryset.count()} to ConfigurationItem queryset")
            queryset = ConfigurationItem.objects.filter(configuration__in=queryset)
            print(f"New queryset size: {queryset.count()}")
        return super().export(queryset, *args, **kwargs)

# Testing the logic
configs = Configuration.objects.all()[:1]
if configs.exists():
    resource = EnhancedConfigurationItemResource()
    dataset = resource.export(configs)
    print(f"Exported {len(dataset)} rows")
else:
    print("No configurations found to test.")
