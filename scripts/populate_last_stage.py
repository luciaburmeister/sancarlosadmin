import os
import django
from django.utils import translation

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'alumniniooerp.settings')
django.setup()

from production.models import ProductionTicket

translation.activate('es')

tickets = ProductionTicket.objects.all()
total = tickets.count()
print(f"Updating {total} tickets in Spanish locale...")

for i, ticket in enumerate(tickets):
    try:
        ticket.recompute_status()
        if (i + 1) % 10 == 0:
            print(f"Processed {i + 1}/{total}")
    except Exception as e:
        print(f"Error updating ticket {ticket.pk}: {e}")

print("Done!")
