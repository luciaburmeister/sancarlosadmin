from typing import Any, Dict

class BaseExportStrategy:
    """
    Abstract base class for export strategies.
    """
    def __init__(self, obj: Any):
        self.obj = obj

    def get_context(self) -> Dict[str, Any]:
        """
        Returns the context for template rendering.
        """
        raise NotImplementedError

    def get_product(self):
        """
        Returns the product associated with the object.
        """
        raise NotImplementedError
