# Mapa de Módulos — Polipus ERP

> **Última actualización:** 2026-05-13

---

## Equipo responsable

***La responsabilidad es el valor y la cualidad humana de cumplir con obligaciones, compromisos y deberes, asumiendo conscientemente las consecuencias de los propios actos y decisiones. Implica responder ante uno mismo y ante otros, actuando con rectitud, honestidad y cuidado, ya sea en el ámbito personal, laboral o social asumiendo CONCIENTEMENTE  las consecuencias de los propios actos y decisiones. Implica responder ante uno mismo y ante otros…***

| Integrante | Rol |
|--------|---------|
| José Antonio García | Director - No solo player |
| Miguel | Ingeniero de campo | 
| Conchi | Ingeniero de campo | 
| José Murillo | Desarrollador | 
| Fran | Desarrollador |
| Andrea (Checar asumir responsabilidad) | Desarrollador | 
| Maria (Checar asumir responsabilidad) | Desarrollador | 
| Lucia (Checar asumir responsabilidad) | Desarrollador | 
| Sergio | Checar que se puede hacer | 


## Componentes externos necesarios

| Integrante | Rol |
|--------|---------|
| Carmen | Consultora laboral |
| Marcos | Tesoreria | 


## Configurador

**Descripción:** Motor de configuración de productos parametrizados. Permite definir templates de producto con parámetros dinámicos, reglas BOM (lista de materiales) con expresiones evaluables, reglas de pricing encadenadas, variables computadas y templates de exportación. Es el corazón del sistema de presupuestos.


**Lo que tiene:**
- Motor de evaluación para expresiones dinámicas
- Servicio de exportación PDF de configuraciones
- Deep clone de product templates
- Flujo de estados: Draft → Completed → Converted → Production Done
- Generación de formulario dinamico simple


**Plan — Lo que falta:**

---

## Facturas

**Descripción:** Gestión completa de facturación de compra y venta. Incluye OCR para lectura automática de facturas de proveedor, sistema de impuestos genérico e interconectado, procesamiento automático de emails IMAP para descarga de facturas, y generación de facturas de venta con numeración automática y estados proforma/final.

**Lo que tiene:**
- OCR + clasificación contable automática
- Procesador de emails IMAP para descarga automática de facturas
- Generación de facturas de venta desde configuraciones y desde Expedientes
- Wizard de registro de pagos
- Import masivo desde Excel
- Tasks Celery para procesamiento OCR asíncrono

**Plan — Lo que falta:**

- [Andrea]  Extracción de bancos Nuek <https://www.nuek.com/> Andrea
- [Andrea]  Conciliación automatica de pagos
- [Andrea-José]  Conexión a contabilidad
- [Andrea-José]  Reportes de cuentas corrientes
- [Andrea-José] Integración con Verifactu

---

## Producción

**Descripción:** Gestión de producción con tickets, items, sesiones de trabajo y planificación avanzada con OR-Tools. Un ticket de producción se genera automáticamente desde una configuración y contiene items (componentes a fabricar) con su planning por etapas. Incluye scheduler que optimiza la asignación de recursos (empleados y máquinas) a sesiones planificadas.

**Lo que tiene:**
- Conversión automática configuración → producción
- Planificador OR-Tools (CP-SAT) para optimización
- Recompute automático de status ticket ↔ configuration
- Tasks Celery para planificación asíncrona
- Seguimiento live de producción

**Plan — Lo que falta:**

- [José Murillo] Reporte de productividad de recursos
- [José Murillo] Bloqueo de recursos para planeación
- [Fran] Remover de inventario materiales utilizados
- [José Murillo - Miguel] Control de producción vs planning (Agenda)
- [José Murillo] En el planificador tener tiempos dependientes de variables
- [José] - Generación de fichas de producto
- [Jose + Miguel] Poner planeación en Aluares
---

## Ventas

**Descripción:** Gestión comercial con clientes, presupuestos (quotes), pedidos y albaranes de entrega. El presupuesto (Quote) es el modelo activo principal, que agrupa configuraciones y calcula totales con impuestos. Los clientes tienen direcciones múltiples, contactos, cuentas bancarias, zonas de venta y agentes comerciales con comisión.

**Lo que tiene:**
- Admin completo con filtros, inlines, autocomplete
- Dashboard de presupuestos 
- Agrupación de configuraciones en quotes para "partidas"
- Albaranes de entrega con numeración

**Plan — Lo que falta:**

- [Maria] Implementar albaranes de entrega en ERP
- [Miguel] Implementar en Fistex Energy
- [Conchi] Implementar en Taller
- [Miguel] Implementar en FISLAV
- [?] Planificación comercial a través de capacidad de producción
- [Fran] Modulo de feedback de presupuestos
- [Fran] Implementación de agentes comerciales y comisiones
- [José] Mandar presupuestos por sistema
- [José] Si despues de 72 horas no se ha actualizado mandar alerta para feedback


---

## Compañia

**Descripción:** Módulo fundacional del que dependen todos los demás. Define la entidad Company, el modelo abstracto `CompanyOwnedModel` para multi-tenancy, empleados, máquinas, configuración de email, zonas de trabajo, horarios, cuentas bancarias, control de asistencia con geolocalización, y gestión documental con tipos, periodicidad y recordatorios de expiración.


**Lo que tiene:**
- Sistema de documentos con expiración y recordatorios por email
- Control de asistencia con geolocalización (clock in/out)
- Horarios de empresa configurables por día
- Zonas de trabajo con geolocalización (OpenStreetMap)

**Plan — Lo que falta:**

- [José Murillo] Terminos y condiciones por compañia, firmados por cliente
- [Maria] Solicitudes de enfermedad 
- [Maria] Permisos retribuidos, no retribuidos 
- [Maria] Dias libres por convenio
- [Maria] Calendarios laborales
- [Maria + Carmen] Reportes de horas por personas
- [] Modulo bajas laborales (Enfermedad, Accidente de Trabajo, Maternidad, Paternidad) - Fecha de inicio y fin
- [] Documentos a cada baja con sus tipos de documentos
- [] Agregar dirección postal
- [] Modulo puestos de trabajos

## Materiales

**Descripción:** Catálogo de materiales y componentes con motor de precios dinámico. Un Material es la materia prima con referencia y coste del proveedor. Un Componente es un building block reutilizable que puede tener múltiples materiales y reglas de precio condicionales. 
Los precios se propagan automáticamente: 
Material → Componente → Configuration.

**Lo que tiene:**
- Motor de precios con safe_eval para expressions dinámicas
- Reglas de precio condicionales por componente
- Cascada automática de precios al cambiar costes
- Providers con emails asociados para matching automático de facturas

**Plan — Lo que falta:**

- [Fran] Pedidos automatico de materiales


---

## Vacaciones

**Descripción:** Gestión completa de vacaciones y ausencias con patrón wallet. Cada empleado tiene un wallet por año con movimientos (adquisición, consumo, ajuste). Las solicitudes de vacaciones pasan por un flujo de aprobación y al aprobarse generan automáticamente un movimiento de consumo. La cancelación genera un ajuste positivo de devolución.


**Lo que tiene:**
- Wallet pattern con movimientos auditables
- Flujo de aprobación/cancelación
- Cálculo automático de días laborables
- Grant automático de días al crear wallet
- Tipos de ausencia configurables por política

**Plan — Lo que falta:**

- [Carmen] Validación de modulo  

---

## API

**Descripción:** API REST con Django REST Framework y autenticación JWT (SimpleJWT). Expone endpoints para la app móvil y frontends externos. Cubre clientes, configuraciones, quotes, productos, etapas de producción, empleados, facturas, expedientes y perfil de usuario.
Tiene que ser una API robusta, escalable, segura y bien documentada. Tiene que tener la capcidad de ser dinamicamente escalada

**Endpoints principales:** Clients, Configurations, Quotes, Products, Production Stages, Employees, Invoices, Project Files, Me (perfil)

**Lo que tiene:**
- Serializers completos 
- Autenticación JWT con tokens de 12h
- Views modulares por dominio

**Plan — Lo que falta:**

- [José Murillo] Generación de modulación standard para integraciones

---

## Contabilidad

**Descripción:** Contabilidad de doble partida basada en el Plan General Contable (PGC) español. Soporta templates de plan contable (PGC, PGC Pymes), cuentas de empresa mapeadas al template, años fiscales, diarios, asientos con líneas debe/haber, y reporting lines para informes internos. Los asientos posted son inmutables y se numeran automáticamente.

**Lo que tiene:**
- Generación automatica de cuentas installer (carga automática de PGC)
- Journal generator (asientos automáticos desde facturas)
- Validación estricta: partida doble, bloqueo de posted, lock date, fiscal year
- Numeración automática de asientos por diario/año

**Plan — Lo que falta:**

- [Maria] Integración con Gastos
- [Maria + Carmen] Integración con Seguridad Social
- [Maria + Carmen] Integración con Nominas

---

## Inventario

**Descripción:** Gestión de almacenes, ubicaciones, items y transacciones de stock. Los items se clasifican en stockable, service o finished product, con generación automática de código. Las transacciones registran entradas y salidas al documento origen (PO, ticket de producción, etc.). Stock on hand se calcula como sum de quantity_change.


**Lo que tiene:**
- Admin con filtros y autocomplete
- Generación automática de códigos (STK-, SRV-, FP-)
- Transacciones con referencia genérica a documento origen
- Integración con purchasing (recepción → stock in)

**Plan — Lo que falta:**

- [Miguel] Pruebas con inventario real
- [Fran] Conexión a producción para quitar items automaticos
- [Fran] Calculo de mérma configurable
- [Miguel] Implementación de merma configurable

---

## Compras

**Descripción:** Gestión de órdenes de compra con flujo completo: creación → aprobación → recepción. Al recibir mercancía se genera automáticamente una ItemTransaction en inventario. Las PO se cierran automáticamente cuando todos los items están completamente recibidos.

**Lo que tiene:**
- Numeración automática de POs y recibos
- Integración con inventario (recepción → ItemTransaction)
- Cierre/reapertura automática de PO según cantidades recibidas
- Soporte para adjuntar presupuesto del proveedor


**Plan — Lo que falta:**

- [José Murillo] Flujo de generación de solicitud de compras de materiales sin precios
- [José Murillo] Niveles de aprobación (Modulo re-usable y configurable para aprobaciones general)
- [José Murillo] Compras automaticas dependiendo de niveles de stock
- [José Murillo] Archivos como evidencia de trabajo (imagenes, pdfs, etc..) con categorizaciones dinamicas por empresa

---

## Pagos

**Descripción:** Registro de cobros y pagos con asignación a facturas. Un Payment puede ser inbound (cobro) o outbound (pago) y se asigna a facturas de compra o venta mediante PaymentAllocation (GenericFK). Al asignar/desasignar, se recalcula automáticamente el payment_status de la factura asociada.

**Lo que tiene:**
- Modulo general de pagos
- Un pago puede ser dividido y adjudicado a cualquier elemento (Factura, Gasto, etc)

**Plan — Lo que falta:**

- [Andrea] Generación de gastos generales para ligar pagos
- [Andrea + José] Importación de transacciones de bancos configurables por compañia con periocidad especifica

---

## Gestión central

**Descripción:** Gestión de expedientes (ProjectFile) como hub central del ciclo de vida de un proyecto. Un expediente agrupa presupuestos, facturas de compra, facturas de venta, tickets de producción y albaranes de entrega bajo una misma referencia. Referencias auto-incrementales por empresa.

**Lo que tiene:**
- Generación automática de referencias (EXP-00001)
- Facturación ligada directamente a Project File

**Plan — Lo que falta:**

- [José] Generación de facturas automaticas dependiendo de hitos de producción
- [Lucia + José] Ligar movimientos logisticos para facturas (Cubas, [Modulo reutilizable])
- Prueba piloto con Rayo cubas

---

## Assets

**Descripción:** Gestión de activos de la empresa (vehículos, maquinaria, etc.) con tipos, planificación y transacciones. Los activos pueden vincularse a configuraciones.

**Lo que tiene:**
- Modulo de GPS y zonas de trabajo para seguimiento de activos

**Plan — Lo que falta:**

- [Lucia + SAN CARLOS] Poner aplicación de las cubas live

---

## Nomina

**Descripción:** Gestión de nóminas basada en el sistema  A3. Modela períodos de nómina, conceptos salariales (devengos y deducciones), nóminas por empleado con desglose de seguridad social, convenios colectivos con categorías profesionales, tablas de antigüedad y pagas extra.

**Lo que tiene:**
- Modelos detallados de convenio colectivo (categorías, pagas extra, antigüedad)
- Configuración de empresa para nómina (CCC, CNAE, AT/EP)
- Perfil de nómina por empleado

**Plan — Lo que falta:**

- [Maria] Cálculo de finiquitos 
- [Maria] Base de datos de convenios colectivos
- [Maria] Bot de descarga de convenios colectivos

## Cuadros de mando

**Descripción:** Cuadros de mando para visualización en tiempo real de la empresa


**Lo que tiene:**

**Plan — Lo que falta:**

- [] Cuadro de mando global
- [José] Cuadro de mando Tesoreria
- [] Cuadro de mando Producción
- [] Cuadro de mando Ventas
- [] Cuadro de mando Comercial
- [] Cuadro de mando Administrativo

---
---

## Sistema de tickets
- Implementación de sistema de tickets interno de gestión con su debida resolución interno.
- Proceso de nueva configuración por empresa - Enrique y Aluares

# 📋 Plan de Desarrollo

## Visión general

El plan se organiza en **5 fases** basadas en dependencias técnicas y prioridad de negocio. Cada fase tiene un objetivo claro y un conjunto de entregables.

> ⚠️ **Desarrollo ≠ Implementación.** Muchas tareas de fases distintas se pueden **desarrollar en paralelo** porque no comparten código ni personas. La fase indica cuándo se **despliega e implementa en producción**, no cuándo se empieza a construir. Por ejemplo, Maria puede estar desarrollando nómina (Fase 4) mientras Andrea trabaja en bancos (Fase 1) y Fran en inventario (Fase 2) — las tres avanzan simultáneamente, pero cada una se implementa cuando sus dependencias técnicas están listas.

```
Desarrollo:  ████████████████████████████████  (todo en paralelo donde sea posible)

Implementación:
Fase 1: Cimientos financieros          ██████████░░░░░░░░░░░░░░░░░░░░
Fase 2: Cadena de suministro           ░░░░░░████████████░░░░░░░░░░░░
Fase 3: Operaciones y expansión        ░░░░░░░░░░░░████████████░░░░░░
Fase 4: RRHH y Nómina                  ░░░░░░░░░░░░░░░░████████░░░░░░
Fase 5: Inteligencia y Dashboards      ░░░░░░░░░░░░░░░░░░░░░░████████
```

### Pistas de desarrollo paralelo

Estas pistas pueden avanzar simultáneamente porque no comparten dependencias técnicas entre sí:

| Pista | Personas | Tareas | Implementa en |
|-------|----------|--------|---------------|
| **A — Financiero** | Andrea, José | 1.1→1.10 | Fase 1 |
| **B — Supply Chain** | Fran, Miguel, José Murillo | 2.1→2.10 | Fase 2 |
| **C — Comercial** | Fran, Miguel, Conchi | 3.1→3.7 | Fase 3 |
| **D — Producción avanzada** | José Murillo, José, Miguel | 3.8→3.13 | Fase 3 |
| **E — Logística & Assets** | Lucia, José | 3.15, 3.17 | Fase 3 |
| **F — Nómina** | Maria, Carmen | 4.1→4.7 | Fase 4 |
| **G — Dashboards** | Varios | 5.1→5.6 | Fase 5 |

---

## Fase 1 — Cimientos financieros

> **Objetivo:** Cerrar el ciclo financiero completo: factura → pago → asiento contable → reporting

**¿Por qué primero?** Todo lo demás (inventario, producción, nómina) genera asientos contables. Sin esta base, el resto crea datos huérfanos.

| # | Tarea | Responsable | Módulo | Depende de | Estado |
|---|-------|-------------|--------|------------|--------|
| 1.1 | Extracción de bancos Nuek | Andrea | Facturas | — | ⬜ Pendiente |
| 1.2 | Importación de transacciones bancarias configurables por compañía | Andrea + José | Pagos | 1.1 | ⬜ Pendiente |
| 1.3 | Generación de gastos generales para ligar pagos | Andrea | Pagos | — | ⬜ Pendiente |
| 1.4 | Cerebro de reconciliación automática de pagos | Andrea | Pagos | 1.1, 1.2, 1.3 | ⬜ Pendiente |
| 1.5 | Conciliación automática de pagos con facturas | Andrea | Facturas | 1.4 | ⬜ Pendiente |
| 1.6 | Conexión facturación → contabilidad (asientos automáticos) | Andrea + José | Facturas / Contabilidad | — | ⬜ Pendiente |
| 1.7 | Integración con Gastos → Contabilidad | Maria | Contabilidad | 1.3, 1.6 | ⬜ Pendiente |
| 1.8 | Reportes de cuentas corrientes | Andrea + José | Facturas | 1.6 | ⬜ Pendiente |
| 1.9 | Integración con Verifactu | Andrea + José | Facturas | 1.6 | ⬜ Pendiente |
| 1.10 | Cuadro de mando: Pagos, CC y Tesorería | José | Cuadros de mando | 1.4, 1.8 | ⬜ Pendiente |

**Hito Fase 1:** ✅ Un ciclo completo de factura → pago reconciliado → asiento contable funciona sin intervención manual

---

## Fase 2 — Cadena de suministro

> **Objetivo:** Cerrar el ciclo de materiales: demanda de producción → compra → recepción → stock → consumo en producción → merma

**¿Por qué segundo?** La producción ya funciona pero no descuenta materiales. Sin esto, el inventario es ficción.

| # | Tarea | Responsable | Módulo | Depende de | Estado |
|---|-------|-------------|--------|------------|--------|
| 2.1 | Pruebas con inventario real | Miguel | Inventario | — | ⬜ Pendiente |
| 2.2 | Conexión producción → inventario (descontar items automático) | Fran | Inventario / Producción | 2.1 | ⬜ Pendiente |
| 2.3 | Cálculo de merma configurable | Fran | Inventario | 2.2 | ⬜ Pendiente |
| 2.4 | Implementación de merma configurable (campo) | Miguel | Inventario | 2.3 | ⬜ Pendiente |
| 2.5 | Pedidos automáticos de materiales | Fran | Materiales | 2.2 | ⬜ Pendiente |
| 2.6 | Flujo solicitud de compras sin precios | José Murillo | Compras | 2.5 | ⬜ Pendiente |
| 2.7 | Niveles de aprobación (módulo reutilizable) | José Murillo | Compras | 2.6 | ⬜ Pendiente |
| 2.8 | Compras automáticas por niveles de stock | José Murillo | Compras | 2.5, 2.7 | ⬜ Pendiente |
| 2.9 | Remover de inventario materiales utilizados en producción | Fran | Producción | 2.2 | ⬜ Pendiente |
| 2.10 | Cuadro de mando: Almacén | Fran | Cuadros de mando | 2.2, 2.4 | ⬜ Pendiente |

**Hito Fase 2:** ✅ Al completar producción de un ticket, los materiales se descuentan automáticamente del inventario y se generan alertas de reposición

---

## Fase 3 — Operaciones comerciales y producción avanzada

> **Objetivo:** Escalar el sistema a múltiples empresas/sectores y mejorar la operativa de producción

**¿Por qué tercero?** Requiere que la base financiera (Fase 1) y la cadena de suministro (Fase 2) estén estables.

| # | Tarea | Responsable | Módulo | Depende de | Estado |
|---|-------|-------------|--------|------------|--------|
| 3.1 | Implementar configuración en FISLAV | Miguel | Ventas | — | ⬜ Pendiente |
| 3.2 | Implementar albaranes de entrega en ERP | Maria | Ventas | — | ⬜ Pendiente |
| 3.3 | Implementar configuración en Fistex Energy | Miguel | Ventas | — | ⬜ Pendiente |
| 3.4 | Implementar configuración en Taller | Conchi | Ventas | — | ⬜ Pendiente |
| 3.5 | Módulo de feedback de presupuestos | Fran | Ventas | — | ⬜ Pendiente |
| 3.6 | Implementación de agentes comerciales y comisiones | Fran | Ventas | — | ⬜ Pendiente |
| 3.7 | Planificación comercial a través de capacidad de producción | ? | Ventas / Producción | 3.9 | ⬜ Pendiente |
| 3.8 | Reporte de productividad de recursos | José Murillo | Producción | — | ⬜ Pendiente |
| 3.9 | Bloqueo de recursos para planeación | José Murillo | Producción | — | ⬜ Pendiente |
| 3.10 | Control de producción vs planning (Agenda) | José Murillo + Miguel | Producción | 3.9 | ⬜ Pendiente |
| 3.11 | Tiempos dependientes de variables en planificador | José Murillo | Producción | 3.9 | ⬜ Pendiente |
| 3.12 | Generación de fichas de producto | José | Producción | — | ⬜ Pendiente |
| 3.13 | Poner planeación en Aluares | José + Miguel | Producción | 3.9 | ⬜ Pendiente |
| 3.14 | Generación de facturas automáticas por hitos de producción | José | Gestión central | Fase 1 | ⬜ Pendiente |
| 3.15 | Ligar movimientos logísticos para facturas (Cubas, módulo reutilizable) | Lucia + José | Gestión central | — | ⬜ Pendiente |
| 3.16 | Generación de modulación standard para integraciones API | José Murillo | API | — | ⬜ Pendiente |
| 3.17 | Poner aplicación de las cubas live | Lucia + SAN CARLOS | Assets | — | ⬜ Pendiente |

**Hito Fase 3:** ✅ (Aluares, Energy, FISLAV, San Carlos) operando con el ERP completo, con agentes comerciales

---

## Fase 4 — RRHH y Nómina

> **Objetivo:** Automatizar el ciclo laboral: convenio → nómina → contabilidad → seguridad social

**¿Por qué cuarto?** Es un bloque independiente que requiere validación externa (Carmen) y tiene su propio ritmo.

| # | Tarea | Responsable | Módulo | Depende de | Estado |
|---|-------|-------------|--------|------------|--------|
| 4.1 | Validación del módulo de vacaciones | Carmen | Vacaciones | — | ⬜ Pendiente |
| 4.2 | Base de datos de convenios colectivos | Maria | Nómina | — | ⬜ Pendiente |
| 4.3 | Bot de descarga de convenios colectivos | Maria | Nómina | 4.2 | ⬜ Pendiente |
| 4.4 | Cálculo automático de nómina | Maria | Nómina | 4.2 | ⬜ Pendiente |
| 4.5 | Cálculo de finiquitos | Maria | Nómina | 4.4 | ⬜ Pendiente |
| 4.6 | Integración Nóminas → Contabilidad | Maria + Carmen | Contabilidad | 4.4, Fase 1 | ⬜ Pendiente |
| 4.7 | Integración Seguridad Social → Contabilidad | Maria + Carmen | Contabilidad | 4.4, Fase 1 | ⬜ Pendiente |

**Hito Fase 4:** ✅ Nóminas mensuales generadas automáticamente desde convenio, con asientos contables y ficheros de SS

---

## Fase 5 — Inteligencia y Dashboards

> **Objetivo:** Visibilidad completa del negocio en tiempo real para toma de decisiones

**¿Por qué último?** Los dashboards necesitan datos reales fluyendo por todos los módulos anteriores.

| # | Tarea | Responsable | Módulo | Depende de | Estado |
|---|-------|-------------|--------|------------|--------|
| 5.1 | Cuadro de mando: Pagos, CC y Tesorería | José | Cuadros de mando | Fase 1 | ⬜ Pendiente |
| 5.2 | Cuadro de mando: Producción | ? | Cuadros de mando | Fase 2, 3.8 | ⬜ Pendiente |
| 5.3 | Cuadro de mando: Ventas | ? | Cuadros de mando | Fase 3 | ⬜ Pendiente |
| 5.4 | Cuadro de mando: Comercial | ? | Cuadros de mando | 3.6 | ⬜ Pendiente |
| 5.5 | Cuadro de mando: Administrativo | ? | Cuadros de mando | Fase 1, 4 | ⬜ Pendiente |
| 5.6 | Cuadro de mando: Almacén | Fran | Cuadros de mando | Fase 2 | ⬜ Pendiente |

**Hito Fase 5:** ✅ Dirección puede ver el estado completo de la empresa desde un único panel sin preguntar a nadie

---

## Resumen de carga por persona

| Persona | Fase 1 | Fase 2 | Fase 3 | Fase 4 | Fase 5 | Total |
|---------|--------|--------|--------|--------|--------|-------|
| **Andrea** | 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.8, 1.9 | — | — | — | — | 8 |
| **José Murillo** | — | 2.6, 2.7, 2.8 | 3.8, 3.9, 3.10, 3.11, 3.16 | — | — | 8 |
| **Fran** | — | 2.2, 2.3, 2.5, 2.9, 2.10 | 3.5, 3.6 | — | 5.6 | 8 |
| **Maria** | 1.7 | — | 3.1 | 4.2, 4.3, 4.4, 4.5, 4.6, 4.7 | — | 8 |
| **Miguel** | — | 2.1, 2.4 | 3.2, 3.4, 3.10, 3.13 | — | — | 6 |
| **José** | 1.6, 1.8, 1.9, 1.10 | — | 3.12, 3.13, 3.14, 3.15 | — | 5.1 | 9 |
| **Lucia** | — | — | 3.15, 3.17 | — | — | 2 |
| **Conchi** | — | — | 3.3 | — | — | 1 |
| **Carmen** | — | — | — | 4.1, 4.6, 4.7 | — | 3 |
| **Sergio** | — | — | — | — | — | — |