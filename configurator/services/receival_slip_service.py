from django.db import transaction
from django.utils import timezone

class ReceivalSlipService:
    @classmethod
    @transaction.atomic
    def generate_next_reference(cls, company):
        from configurator.models import ReceivalSlip
        """
        Generates the next sequential reference for a company.
        Format: ALB-RX-00001
        """
        # We look for the highest numerical part in the existing references for this company
        last_slip = ReceivalSlip.objects.filter(
            company=company,
            reference__startswith="ALB-RX-"
        ).select_for_update().order_by('-pk').first()

        next_id = 1
        if last_slip and last_slip.reference:
            try:
                # Extract the number from "ALB-RX-XXXXX"
                parts = last_slip.reference.split('-')
                if len(parts) >= 3:
                     next_id = int(parts[-1]) + 1
            except (ValueError, IndexError):
                pass
        
        return f"ALB-RX-{next_id:05d}"

    @classmethod
    @transaction.atomic
    def create_from_configuration(cls, configuration):
        from configurator.models import ReceivalSlip
        """
        Creates a ReceivalSlip from a Configuration.
        """
        reference = cls.generate_next_reference(configuration.company)
        
        slip = ReceivalSlip.objects.create(
            company=configuration.company,
            configuration=configuration,
            reference=reference,
            date=timezone.localdate(),
        )
        
        return slip
