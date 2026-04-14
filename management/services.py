from django.db import transaction

class ProjectFileService:

    @classmethod
    @transaction.atomic
    def generate_next_reference_data(cls, company):
        from management.models import ProjectFile
        """
        Generates the next sequential reference and incremental ID for a company.
        Format: EXP-XXXXX (e.g., EXP-00001)
        """
        # Lock the last project for this company for update
        last_project = ProjectFile.objects.filter(
            company=company,
            incremental_id__isnull=False
        ).select_for_update().order_by('-incremental_id').first()

        next_id = (last_project.incremental_id + 1) if last_project else 1
        
        return {
            'reference': f"EXP-{next_id:05d}",
            'incremental_id': next_id
        }
