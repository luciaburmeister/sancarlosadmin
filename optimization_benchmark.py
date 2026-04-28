from datetime import date, time, timedelta
from decimal import Decimal
import base64
import time as pytime

from django.core.files.base import ContentFile
from django.db import transaction

from django.contrib.contenttypes.models import ContentType

from djmoney.money import Money

from company.models import (
    Company,
    CompanyScheduleSlot,
    MachineType,
    Machine,
    EmployeeType,
    Employee,
)
from configurator.models import (
    ProductTemplate,
    Configuration,
    ConfigurationItem,
    ConfigurationStatus,
)
from production.models import ProductionResource
from production.services.scheduler.schedule_margin import MarginScheduler
from materials.models import Component


PREFIX_V3 = "OBV3"


def next_weekday(start: date, weekday: int) -> date:
    days_ahead = (weekday - start.weekday()) % 7
    return start + timedelta(days=days_ahead)


def tiny_png_file(name="benchmark_logo.png"):
    png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
        "/w8AAn8B9p4mVwAAAABJRU5ErkJggg=="
    )
    return ContentFile(base64.b64decode(png_b64), name=name)


def benchmark_dates():
    start = next_weekday(date.today(), 0)  # next Monday
    return {
        "day_1_start": start,
        "day_2_end": start + timedelta(days=1),
        "day_3_end": start + timedelta(days=2),
        "day_5_end": start + timedelta(days=4),
    }


def _upsert_production_resource(company, content_type, object_id, daily_capacity_minutes=480, parallel_capacity=1):
    ProductionResource.objects.update_or_create(
        content_type=content_type,
        object_id=object_id,
        defaults={
            "company": company,
            "daily_capacity_minutes": daily_capacity_minutes,
            "parallel_capacity": parallel_capacity,
            "is_active": True,
        },
    )


def _pick_components(n=20):
    components = list(Component.objects.order_by("id")[:n])
    if not components:
        return [None] * n
    while len(components) < n:
        components.append(components[0])
    return components[:n]


def stage_info(machine_type_id, employee_type_id, total_time_min):
    return {
        "machine_types": [{"quantity": 1, "machine_type_id": machine_type_id}],
        "employee_types": [{"quantity": 1, "employee_type_id": employee_type_id}],
        "prep_time_min": 0,
        "work_time_min": total_time_min,
        "post_time_min": 0,
        "total_time_min": total_time_min,
    }


def make_item_stage_resources(mt_cut, mt_mach, mt_paint, mt_assy, et_cut, et_mach, et_paint, et_assy,
                              cut=None, machining=None, paint=None, assembly=None):
    data = {}
    if cut is not None:
        data["CUT"] = stage_info(mt_cut.id, et_cut.id, cut)
    if machining is not None:
        data["MACHINING"] = stage_info(mt_mach.id, et_mach.id, machining)
    if paint is not None:
        data["PAINT"] = stage_info(mt_paint.id, et_paint.id, paint)
    if assembly is not None:
        data["ASSEMBLY"] = stage_info(mt_assy.id, et_assy.id, assembly)
    return data


def cleanup_benchmark_v3():
    Company.objects.filter(name__startswith=PREFIX_V3).delete()


def _create_base_company_and_resources():
    company = Company(
        name=f"{PREFIX_V3}_COMPANY",
        legal_name=f"{PREFIX_V3}_COMPANY_SL",
        is_active=True,
        production_stages=["CUT", "MACHINING", "PAINT", "ASSEMBLY"],
    )
    company.logo.save(f"{PREFIX_V3.lower()}_logo.png", tiny_png_file(), save=False)
    company.save()

    for weekday in range(5):
        CompanyScheduleSlot.objects.create(
            company=company,
            weekday=weekday,
            start_time=time(9, 0),
            end_time=time(17, 0),
        )

    mt_cut = MachineType.objects.create(company=company, type="OB3_CUT_M")
    mt_mach = MachineType.objects.create(company=company, type="OB3_MACH_M")
    mt_paint = MachineType.objects.create(company=company, type="OB3_PAINT_M")
    mt_assy = MachineType.objects.create(company=company, type="OB3_ASSY_M")

    et_cut = EmployeeType.objects.create(company=company, type="OB3_CUT_E")
    et_mach = EmployeeType.objects.create(company=company, type="OB3_MACH_E")
    et_paint = EmployeeType.objects.create(company=company, type="OB3_PAINT_E")
    et_assy = EmployeeType.objects.create(company=company, type="OB3_ASSY_E")

    m_cut = Machine.objects.create(
        company=company, name="OB3_CUT_01", code="OB3_CUT_01",
        type=mt_cut, is_active=True, needs_human=True,
        daily_capacity_minutes=480, parallel_capacity=1,
    )
    m_mach = Machine.objects.create(
        company=company, name="OB3_MACH_01", code="OB3_MACH_01",
        type=mt_mach, is_active=True, needs_human=True,
        daily_capacity_minutes=480, parallel_capacity=1,
    )
    m_paint = Machine.objects.create(
        company=company, name="OB3_PAINT_01", code="OB3_PAINT_01",
        type=mt_paint, is_active=True, needs_human=True,
        daily_capacity_minutes=480, parallel_capacity=1,
    )
    m_assy = Machine.objects.create(
        company=company, name="OB3_ASSY_01", code="OB3_ASSY_01",
        type=mt_assy, is_active=True, needs_human=True,
        daily_capacity_minutes=480, parallel_capacity=1,
    )

    hired_at = date.today() - timedelta(days=365)

    e_cut = Employee.objects.create(
        company=company, first_name="OB3", last_name="Cut",
        hired_at=hired_at, is_active=True, can_work_on_production=True,
        identification_number="OB3E01", type=et_cut, production_stages=["CUT"],
    )
    e_mach = Employee.objects.create(
        company=company, first_name="OB3", last_name="Mach",
        hired_at=hired_at, is_active=True, can_work_on_production=True,
        identification_number="OB3E02", type=et_mach, production_stages=["MACHINING"],
    )
    e_paint = Employee.objects.create(
        company=company, first_name="OB3", last_name="Paint",
        hired_at=hired_at, is_active=True, can_work_on_production=True,
        identification_number="OB3E03", type=et_paint, production_stages=["PAINT"],
    )
    e_assy = Employee.objects.create(
        company=company, first_name="OB3", last_name="Assy",
        hired_at=hired_at, is_active=True, can_work_on_production=True,
        identification_number="OB3E04", type=et_assy, production_stages=["ASSEMBLY"],
    )

    machine_ct = ContentType.objects.get_for_model(Machine)
    employee_ct = ContentType.objects.get_for_model(Employee)

    for target in (m_cut, m_mach, m_paint, m_assy):
        _upsert_production_resource(company, machine_ct, target.id, 480, 1)

    for target in (e_cut, e_mach, e_paint, e_assy):
        _upsert_production_resource(company, employee_ct, target.id, 480, 1)

    return {
        "company": company,
        "mt_cut": mt_cut,
        "mt_mach": mt_mach,
        "mt_paint": mt_paint,
        "mt_assy": mt_assy,
        "et_cut": et_cut,
        "et_mach": et_mach,
        "et_paint": et_paint,
        "et_assy": et_assy,
    }


def create_optimization_benchmark_v3():
    """
    Persistent version: writes to DB and stays there until cleanup_benchmark_v3().
    """
    cleanup_benchmark_v3()

    base = _create_base_company_and_resources()
    company = base["company"]

    components = _pick_components(16)

    # 8 configurations, each with 2 items.
    # Expected direction:
    # - 2 days: only top ~3 fit
    # - 3 days: more fit
    # - 5 days: all should fit
    config_specs = [
        # name, margin, item_1 stages, item_2 stages
        ("P1", 1600, {"cut": 180, "machining": 60,  "assembly": 120}, {"cut": 120, "paint": 60,  "assembly": 60}),
        ("P2", 1400, {"cut": 180, "paint": 120, "assembly": 60},      {"cut": 120, "assembly": 120}),
        ("P3", 1300, {"cut": 120, "machining": 120, "assembly": 60},  {"cut": 120, "paint": 60,  "assembly": 60}),
        ("P4", 1200, {"cut": 180, "assembly": 120},                   {"cut": 60,  "paint": 120, "assembly": 60}),
        ("P5", 1100, {"cut": 120, "machining": 60, "paint": 60},      {"cut": 120, "assembly": 120}),
        ("P6", 1000, {"cut": 180, "paint": 60, "assembly": 60},       {"cut": 60,  "assembly": 120}),
        ("P7", 900,  {"cut": 120, "machining": 60, "assembly": 120},   {"cut": 60,  "paint": 60, "assembly": 60}),
        ("P8", 800,  {"cut": 60,  "assembly": 180},                    {"cut": 120, "paint": 60, "assembly": 60}),
    ]

    created_configs = []

    for idx, (short_name, margin, item1, item2) in enumerate(config_specs):
        product = ProductTemplate.objects.create(
            company=company,
            name=f"OB3_{short_name}_PRODUCT",
            description=f"Benchmark V3 product {short_name}",
            production_stages=["CUT", "MACHINING", "PAINT", "ASSEMBLY"],
            production_planning_resources={},
            production_planning_configured=True,
        )

        cfg = Configuration.objects.create(
            company=company,
            name=f"OB3_{short_name}",
            product=product,
            status=ConfigurationStatus.DRAFT,
            total_cost=Money(500, "EUR"),
            subtotal_price=Money(500 + margin, "EUR"),
            total_price=Money(500 + margin, "EUR"),
        )

        sr1 = make_item_stage_resources(
            base["mt_cut"], base["mt_mach"], base["mt_paint"], base["mt_assy"],
            base["et_cut"], base["et_mach"], base["et_paint"], base["et_assy"],
            **item1
        )
        sr2 = make_item_stage_resources(
            base["mt_cut"], base["mt_mach"], base["mt_paint"], base["mt_assy"],
            base["et_cut"], base["et_mach"], base["et_paint"], base["et_assy"],
            **item2
        )

        ConfigurationItem.objects.create(
            configuration=cfg,
            component=components[idx * 2],
            quantity=Decimal("1"),
            length=Decimal("1"),
            is_template_item=True,
            unit_price=Money(100, "EUR"),
            production_stages=list(sr1.keys()),
            stage_resources=sr1,
            note=f"{short_name} item 1",
        )

        ConfigurationItem.objects.create(
            configuration=cfg,
            component=components[idx * 2 + 1],
            quantity=Decimal("1"),
            length=Decimal("1"),
            is_template_item=True,
            unit_price=Money(100, "EUR"),
            production_stages=list(sr2.keys()),
            stage_resources=sr2,
            note=f"{short_name} item 2",
        )

        created_configs.append(cfg)

    return {
        "company": company,
        "configurations": created_configs,
    }


def get_benchmark_v3():
    company = Company.objects.get(name=f"{PREFIX_V3}_COMPANY")
    configs = list(
        Configuration.objects.filter(company=company, name__startswith="OB3_").order_by("name")
    )
    return {"company": company, "configurations": configs}


def _run_scheduler(configurations, days=5, ts=30):
    dates = benchmark_dates()
    start = dates["day_1_start"]
    end = start + timedelta(days=days - 1)

    scheduler = MarginScheduler({"ts": ts})

    t0 = pytime.perf_counter()
    proposal = scheduler.schedule(
        start_date=start,
        end_date=end,
        configurations=configurations,
    )
    elapsed = pytime.perf_counter() - t0

    print("\n=== BENCHMARK V3 RUN ===")
    print("Start:", start)
    print("End:  ", end)
    print("Days:", days)
    print("Slot minutes:", ts)
    print("Elapsed seconds:", round(elapsed, 3))
    print("Solver status:", proposal.solver_status)
    print("Is optimal:", proposal.is_optimal)
    print("Objective value:", proposal.objective_value)
    print("Total margin:", proposal.total_margin)

    selected = [c.name for c in configurations if c.pk in proposal.selected_configuration_ids]
    unselected = [c.name for c in configurations if c.pk in proposal.unselected_configuration_ids]

    print("Selected:", selected)
    print("Unselected:", unselected)

    if proposal.warnings:
        print("\nWarnings:")
        for w in proposal.warnings:
            print(" -", w)

    return proposal, elapsed


def run_benchmark_v3(days=5, ts=30):
    bench = get_benchmark_v3()
    return _run_scheduler(bench["configurations"], days=days, ts=ts)


def run_temp_benchmark_v3(days=5, ts=30):
    """
    Temporary version:
    - creates benchmark data
    - runs optimization
    - prints elapsed time
    - rolls everything back so DB stays clean
    """
    with transaction.atomic():
        bench = create_optimization_benchmark_v3()
        proposal, elapsed = _run_scheduler(bench["configurations"], days=days, ts=ts)
        transaction.set_rollback(True)
        print("\nTemporary benchmark rolled back. DB left clean.")
        return proposal, elapsed