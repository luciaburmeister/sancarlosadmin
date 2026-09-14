"""Recompute the derived delivery / production status of project files.

By default only expedientes that are still open (new, active, on hold) are
touched, so historical completed or cancelled files keep whatever they show.
Pass --all to include them.
"""
from django.core.management.base import BaseCommand

from management.models import ProjectFile, ProjectFileStatus
from management.services.project_file_status import recompute_project_file_status


class Command(BaseCommand):
    help = "Recompute delivery_status / production_status (and the lifecycle they imply) for project files."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true", help="Include completed and cancelled project files.")
        parser.add_argument("--company", type=int, help="Restrict to one company id.")

    def handle(self, *args, **options):
        qs = ProjectFile.objects.all()
        if not options["all"]:
            qs = qs.exclude(status__in=[ProjectFileStatus.COMPLETED, ProjectFileStatus.CANCELLED])
        if options.get("company"):
            qs = qs.filter(company_id=options["company"])

        changed = 0
        total = 0
        for pf in qs.only("pk", "status", "delivery_status", "production_status").iterator():
            total += 1
            before = (pf.status, pf.delivery_status, pf.production_status)
            after_pf = recompute_project_file_status(pf.pk)
            after = (after_pf.status, after_pf.delivery_status, after_pf.production_status)
            if before != after:
                changed += 1
                self.stdout.write(f"{after_pf.reference or after_pf.pk}: {before} -> {after}")

        self.stdout.write(self.style.SUCCESS(f"Recomputed {total} project files, {changed} changed."))
