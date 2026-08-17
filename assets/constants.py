MAX_ZONE_RADIUS_M = 1_000

SERVICE_TYPE_MAQUINA_PROPIA = "porte_maquina"
SERVICE_TYPE_MATERIAL_EXTERNO = "porte_material"
SERVICE_TYPE_MATERIAL_PROPIO = "porte_material_propio"
SERVICE_TYPE_GRUA = "servicio_grua"
SERVICE_TYPE_CUBA = "movimiento_cuba"

# Backend planning_type ("cuba"/"porte", consumed by AssetPlanning and the
# mobile API) derived from the requester-facing service_type.
SERVICE_TYPE_PLANNING_TYPE = {
    SERVICE_TYPE_MAQUINA_PROPIA: "porte",
    SERVICE_TYPE_MATERIAL_EXTERNO: "porte",
    SERVICE_TYPE_MATERIAL_PROPIO: "porte",
    SERVICE_TYPE_GRUA: "porte",
    SERVICE_TYPE_CUBA: "cuba",
}

# Which AssetPlanningRequest fields are relevant for each service_type.
# Drives both AssetPlanningRequest.clean() and the request form's
# show/hide behavior (AssetPlanningRequestAdmin.conditional_fields, unfold's
# native Alpine.js-based mechanism).
SERVICE_TYPE_FIELDS = {
    SERVICE_TYPE_MAQUINA_PROPIA: {"from_zone", "to_zone", "asset", "suggested_trucks"},
    SERVICE_TYPE_MATERIAL_EXTERNO: {"from_zone", "to_zone", "load_description", "suggested_trucks"},
    SERVICE_TYPE_MATERIAL_PROPIO: {"from_zone", "to_zone", "load_description", "suggested_trucks"},
    SERVICE_TYPE_GRUA: {"to_zone", "suggested_trucks"},
    SERVICE_TYPE_CUBA: {"to_zone", "cuba_operation_type", "cuba_size", "suggested_trucks"},
}
