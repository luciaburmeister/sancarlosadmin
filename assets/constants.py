MAX_ZONE_RADIUS_M = 1_000

SERVICE_TYPE_MAQUINA_PROPIA = "porte_maquina"
SERVICE_TYPE_MATERIAL_EXTERNO = "porte_material"
SERVICE_TYPE_MATERIAL_PROPIO = "porte_material_propio"
SERVICE_TYPE_GRUA = "servicio_grua"
SERVICE_TYPE_CUBA = "movimiento_cuba"
SERVICE_TYPE_MAQUINISTAS = "movimiento_maquinistas"
SERVICE_TYPE_AUTOPORTE_MAQUINA = "autoporte_maquina"
SERVICE_TYPE_PEONES = "movimiento_peones"

# Backend planning_type ("cuba"/"porte"/"none", consumed by AssetPlanning and
# the mobile API) derived from the requester-facing service_type. "none"
# means the request never produces an AssetPlanning at all.
SERVICE_TYPE_PLANNING_TYPE = {
    SERVICE_TYPE_MAQUINA_PROPIA: "porte",
    SERVICE_TYPE_MATERIAL_EXTERNO: "porte",
    SERVICE_TYPE_MATERIAL_PROPIO: "porte",
    SERVICE_TYPE_GRUA: "porte",
    SERVICE_TYPE_CUBA: "cuba",
    SERVICE_TYPE_MAQUINISTAS: "none",
    SERVICE_TYPE_AUTOPORTE_MAQUINA: "none",
    SERVICE_TYPE_PEONES: "none",
}

class AssignRequestKey:
    USE_PENDING_CUBA = "use_pending_cuba"
    ASSET_ID = "asset_id"


CUBA_CANDIDATE_LIMIT = 4

# Fields relevant per service_type. Drives AssetPlanningRequest.clean(), the admin
# form's show/hide, and AssetPlanningRequestDraft.missing_fields_for_submit() — only
# add a field name that exists on *both* models, since that method does a plain getattr().
SERVICE_TYPE_FIELDS = {
    SERVICE_TYPE_MAQUINA_PROPIA: {"from_zone", "to_zone", "machine", "suggested_trucks"},
    SERVICE_TYPE_MATERIAL_EXTERNO: {"from_zone", "to_zone", "load_description", "suggested_trucks"},
    SERVICE_TYPE_MATERIAL_PROPIO: {"from_zone", "to_zone", "load_description", "suggested_trucks"},
    SERVICE_TYPE_GRUA: {"to_zone", "suggested_trucks"},
    SERVICE_TYPE_CUBA: {"to_zone", "cuba_operation_type", "cuba_size", "suggested_trucks"},
    # moved_employees handled separately, see SERVICE_TYPES_NEEDING_MOVED_EMPLOYEES.
    SERVICE_TYPE_MAQUINISTAS: {"from_zone", "to_zone"},
    # Self-moving machine, no truck/dispatch — only the destination matters,
    # same shape as SERVICE_TYPE_GRUA minus the truck.
    SERVICE_TYPE_AUTOPORTE_MAQUINA: {"to_zone", "asset"},
    SERVICE_TYPE_PEONES: {"from_zone", "to_zone"},
}

# Service types requiring at least one Employee in moved_employees to submit/approve.
# Separate from SERVICE_TYPE_FIELDS since it's a M2M, not a plain getattr() check.
SERVICE_TYPES_NEEDING_MOVED_EMPLOYEES = frozenset({SERVICE_TYPE_PEONES})
