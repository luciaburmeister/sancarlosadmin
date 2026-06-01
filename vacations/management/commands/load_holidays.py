from django.core.management.base import BaseCommand
from vacations.models import WorkCalendar, PublicHoliday
from vacations.data.holidays import MADRID_HOLIDAYS, MALAGA_HOLIDAYS
from datetime import date


CITY_DATA = {
    "madrid": ("Calendario Madrid", MADRID_HOLIDAYS),
    "malaga": ("Calendario Málaga", MALAGA_HOLIDAYS),
}


class Command(BaseCommand):
    help = "Load public holidays for a given city and year"

    def add_arguments(self, parser):
        parser.add_argument("--year", type=int, required=True)
        parser.add_argument("--city", type=str, required=True, choices=["madrid", "malaga"])

    def handle(self, *args, **options):
        year = options["year"]
        city = options["city"]

        name, holiday_data = CITY_DATA[city]

        if year not in holiday_data:
            self.stdout.write(self.style.ERROR(f"No holiday data found for {city} {year}."))
            return

        calendar, _ = WorkCalendar.objects.get_or_create(
            city=city,
            defaults={"name": f"{name} {year}", "is_active": True},
        )

        created_count = 0
        for date_str, holiday_name, is_national in holiday_data[year]:
            d = date.fromisoformat(date_str)
            _, was_created = PublicHoliday.objects.get_or_create(
                calendar=calendar,
                date=d,
                defaults={"name": holiday_name, "is_national": is_national},
            )
            if was_created:
                created_count += 1

        self.stdout.write(self.style.SUCCESS(
            f"Done. {created_count} holidays loaded for {city} {year}."
        ))