MAX_ZONE_RADIUS_M = 1_000

SERVICE_TYPE_MAQUINA_PROPIA = "porte_maquina"
SERVICE_TYPE_MATERIAL_EXTERNO = "porte_material"
SERVICE_TYPE_MATERIAL_PROPIO = "porte_material_propio"
SERVICE_TYPE_GRUA = "servicio_grua"
SERVICE_TYPE_CUBA = "movimiento_cuba"
SERVICE_TYPE_MAQUINISTAS = "movimiento_maquinistas"

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
}

# Which AssetPlanningRequest fields are relevant for each service_type.
# Drives both AssetPlanningRequest.clean() and the request form's
# show/hide behavior (AssetPlanningRequestAdmin.conditional_fields, unfold's
# native Alpine.js-based mechanism). Also consumed by
# AssetPlanningRequestDraft.missing_fields_for_submit() (mobile drafts) — only
# put a field name here if it exists on *both* models, since that method does
# a plain getattr() over every entry.
SERVICE_TYPE_FIELDS = {
    SERVICE_TYPE_MAQUINA_PROPIA: {"from_zone", "to_zone", "asset", "suggested_trucks"},
    SERVICE_TYPE_MATERIAL_EXTERNO: {"from_zone", "to_zone", "load_description", "suggested_trucks"},
    SERVICE_TYPE_MATERIAL_PROPIO: {"from_zone", "to_zone", "load_description", "suggested_trucks"},
    SERVICE_TYPE_GRUA: {"to_zone", "suggested_trucks"},
    SERVICE_TYPE_CUBA: {"to_zone", "cuba_operation_type", "cuba_size", "suggested_trucks"},
    # No AssetPlanning is created for this type — nothing to schedule/route,
    # so just from/to zone; moved_employees/estimated_duration are handled
    # separately since AssetPlanningRequestDraft has neither field.
    SERVICE_TYPE_MAQUINISTAS: {"from_zone", "to_zone"},
}
