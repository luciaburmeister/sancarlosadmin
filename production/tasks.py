from celery import shared_task
from production.models import SchedulingRun
from production.services.scheduler import build_scheduler_strategy, Scheduler, proposal_to_dict
from configurator.models import Configuration


@shared_task(name="production.tasks.run_schedule_optimization")
def run_schedule_optimization(run_id: int):
    """
    Execute the scheduling optimization in the background.
    Updates the SchedulingRun record with results or error.
    """
    run = SchedulingRun.objects.get(pk=run_id)

    try:
        configuration_ids = run.input_payload.get("configuration_ids", [])
        configurations = list(
            Configuration.objects.filter(pk__in=configuration_ids).select_related("product")
        )

        # Refresh each configuration's planning snapshot from its product
        # template, so the optimizer always sees the template's current
        # stage times and resources instead of a stale copy.
        resynced_items = 0
        for configuration in configurations:
            resynced_items += configuration.resync_planning_from_template()

        strategy = build_scheduler_strategy(
            strategy_code=run.strategy,
            optimization_params={"ts": run.slot_minutes},
        )
        scheduler = Scheduler(strategy)
        proposal = scheduler.schedule(
            start_date=run.start_date,
            end_date=run.end_date,
            configurations=configurations,
        )

        result_payload = proposal_to_dict(proposal)
        result_payload["metadata"]["resynced_items"] = resynced_items
        run.result_payload = result_payload
        run.solver_status = proposal.solver_status
        run.objective_value = proposal.objective_value
        run.total_margin = proposal.total_margin
        run.status = "solved"
        run.save(
            update_fields=[
                "result_payload",
                "solver_status",
                "objective_value",
                "total_margin",
                "status",
            ]
        )
        return {
            "task": "run_schedule_optimization",
            "run_id": run_id,
            "status": "solved",
            "configurations": len(configurations),
            "resynced_items": resynced_items,
            "solver_status": proposal.solver_status,
        }

    except Exception as exc:
        run.status = "error"
        run.error_message = str(exc)
        run.save(update_fields=["status", "error_message"])
        raise
