from configurator.models import Configuration
from configurator.resources.configuration_resource import DetailedItemsResource

resource = DetailedItemsResource()
configs = Configuration.objects.all()[:1]
if configs.exists():
    dataset = resource.export(configs)
    print("Headers in export:")
    print(dataset.headers)
else:
    print("No configurations found.")
