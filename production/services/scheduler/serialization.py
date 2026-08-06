from production.services.scheduler.proposal import ScheduleProposal, ScheduledAssignment
from datetime import datetime


def proposal_to_dict(proposal: ScheduleProposal) -> dict:
    return {
        "start_date": proposal.start_date.isoformat(),
        "end_date": proposal.end_date.isoformat(),
        "slot_minutes": proposal.slot_minutes,
        "solver_status": proposal.solver_status,
        "is_optimal": proposal.is_optimal,
        "objective_value": proposal.objective_value,
        "total_margin": proposal.total_margin,
        "selected_configuration_ids": proposal.selected_configuration_ids,
        "unselected_configuration_ids": proposal.unselected_configuration_ids,
        "configuration_margins": proposal.configuration_margins,
        "unselected_reasons": proposal.unselected_reasons,
        "warnings": proposal.warnings,
        "metadata": proposal.metadata,
        "assignments": [
            {
                "configuration_id": a.configuration_id,
                "configuration_name": a.configuration_name,
                "stage_name": a.stage_name,
                "resource_id": a.resource_id,
                "resource_name": a.resource_name,
                "start": a.start.isoformat(),
                "end": a.end.isoformat(),
                "slot_count": a.slot_count,
                "minutes": a.minutes,
            }
            for a in proposal.assignments
        ],
    }


def proposal_from_dict(data: dict) -> ScheduleProposal:
    return ScheduleProposal(
        start_date=datetime.fromisoformat(data["start_date"]).date(),
        end_date=datetime.fromisoformat(data["end_date"]).date(),
        slot_minutes=data["slot_minutes"],
        solver_status=data["solver_status"],
        is_optimal=data["is_optimal"],
        objective_value=data.get("objective_value"),
        total_margin=data["total_margin"],
        selected_configuration_ids=data.get("selected_configuration_ids", []),
        unselected_configuration_ids=data.get("unselected_configuration_ids", []),
        configuration_margins=data.get("configuration_margins", {}),
        unselected_reasons={
            int(config_id): reasons
            for config_id, reasons in data.get("unselected_reasons", {}).items()
        },
        warnings=data.get("warnings", []),
        metadata=data.get("metadata", {}),
        assignments=[
            ScheduledAssignment(
                configuration_id=a["configuration_id"],
                configuration_name=a["configuration_name"],
                stage_name=a["stage_name"],
                resource_id=a["resource_id"],
                resource_name=a["resource_name"],
                start=datetime.fromisoformat(a["start"]),
                end=datetime.fromisoformat(a["end"]),
                slot_count=a["slot_count"],
                minutes=a["minutes"],
            )
            for a in data.get("assignments", [])
        ],
    )