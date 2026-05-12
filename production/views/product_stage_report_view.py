from collections import defaultdict

from django.http import HttpResponse
from django.template.response import TemplateResponse
from django.utils.translation import gettext_lazy as _
from django.views import View

from unfold.views import UnfoldModelAdminViewMixin

from company.models import Employee
from configurator.models import Configuration, ProductTemplate
from production.models import (
    ProductionItemSession,
    ProductionItemSessionStatus,
    ProductionTicket,
)


class ProductStageReportView(UnfoldModelAdminViewMixin, View):
    title = _("Product stage time report")
    permission_required = ("production.view_productionitemsession",)
    template_name = "admin/production/product_stage_report.html"

    def _get_sessions_qs(self, request):
        """Return a base queryset of completed sessions with optional filters."""
        qs = ProductionItemSession.objects.filter(
            status=ProductionItemSessionStatus.DONE,
            start_time__isnull=False,
            end_time__isnull=False,
        ).select_related(
            "company",
            "production_item__ticket__content_type",
        )

        # Company restriction for non-superusers
        if not request.user.is_superuser:
            employee = (
                Employee.objects.filter(user=request.user, is_active=True)
                .select_related("company")
                .first()
            )
            if employee and employee.company:
                qs = qs.filter(company=employee.company)
            else:
                return qs.none()

        # Apply filters from GET params
        company_id = request.GET.get("company")
        if company_id:
            qs = qs.filter(company_id=company_id)

        product_id = request.GET.get("product")
        date_from = request.GET.get("date_from")
        date_to = request.GET.get("date_to")

        if date_from:
            qs = qs.filter(start_time__date__gte=date_from)
        if date_to:
            qs = qs.filter(start_time__date__lte=date_to)

        # Filter by product — need to go through ticket → configuration
        if product_id:
            from django.contrib.contenttypes.models import ContentType

            config_ct = ContentType.objects.get_for_model(Configuration)
            config_ids = Configuration.objects.filter(
                product_id=product_id
            ).values_list("id", flat=True)
            ticket_ids = ProductionTicket.objects.filter(
                content_type=config_ct,
                object_id__in=config_ids,
            ).values_list("id", flat=True)
            qs = qs.filter(production_item__ticket_id__in=ticket_ids)

        return qs

    def _build_report_data(self, sessions_qs):
        """Aggregate session data into product × stage rows.

        Aggregation is done at the ticket×stage level first:
        all sessions for the same (ticket, stage) are treated as one
        production run, measured from the earliest start to the latest end.
        Then those per-ticket durations are rolled up per product×stage.
        """
        from django.contrib.contenttypes.models import ContentType

        config_ct = ContentType.objects.get_for_model(Configuration)

        # Gather all ticket ids and map them to product names
        ticket_ids = set(
            sessions_qs.values_list(
                "production_item__ticket_id", flat=True
            ).distinct()
        )

        ticket_to_product = {}
        ticket_to_config = {}
        tickets = ProductionTicket.objects.filter(
            id__in=ticket_ids, content_type=config_ct
        )
        config_ids = [t.object_id for t in tickets]
        configs = {
            c.id: c
            for c in Configuration.objects.filter(id__in=config_ids).select_related(
                "product"
            )
        }

        for ticket in tickets:
            config = configs.get(ticket.object_id)
            if config and config.product:
                ticket_to_product[ticket.id] = config.product.name
                ticket_to_config[ticket.id] = config
            else:
                ticket_to_product[ticket.id] = _("Unknown product")

        # Step 1: collect time intervals per (ticket_id, stage)
        # Items processed together will have overlapping intervals —
        # we merge them so concurrent work is only counted once.
        ticket_stage_intervals = defaultdict(list)

        for session in sessions_qs.iterator():
            ticket_id = session.production_item.ticket_id
            stage = session.production_stage
            ticket_stage_intervals[(ticket_id, stage)].append(
                (session.start_time, session.end_time)
            )

        # Merge overlapping intervals and sum actual working time
        ticket_stage_totals = {}
        for key, intervals in ticket_stage_intervals.items():
            intervals.sort(key=lambda x: x[0])
            merged = [intervals[0]]
            for start, end in intervals[1:]:
                prev_start, prev_end = merged[-1]
                if start <= prev_end:
                    # Overlapping or adjacent — extend
                    merged[-1] = (prev_start, max(prev_end, end))
                else:
                    merged.append((start, end))
            ticket_stage_totals[key] = sum(
                (end - start).total_seconds() for start, end in merged
            )

        # Step 2: roll up into product → stage → {total_seconds, count}
        # count = number of tickets (orders) that went through this stage
        aggregated = defaultdict(lambda: defaultdict(lambda: {"total_seconds": 0, "count": 0}))
        product_stages_order = {}
        product_distinct_tickets = defaultdict(set)

        for (ticket_id, stage), duration_seconds in ticket_stage_totals.items():
            product_name = ticket_to_product.get(ticket_id, _("Unknown product"))

            aggregated[product_name][stage]["total_seconds"] += duration_seconds
            aggregated[product_name][stage]["count"] += 1
            product_distinct_tickets[product_name].add(ticket_id)

            # Resolve stage order from product template (once per product)
            if product_name not in product_stages_order:
                config = ticket_to_config.get(ticket_id)
                if config and config.product:
                    product_stages_order[product_name] = list(
                        config.product.production_stages or []
                    )

        # Build flat rows
        rows = []

        for product_name in sorted(aggregated.keys()):
            stage_order = product_stages_order.get(product_name, [])
            stages = aggregated[product_name]
            distinct_count = len(product_distinct_tickets[product_name])

            # Sort stages by product order, then alphabetical for unknowns
            def stage_sort_key(s):
                try:
                    return (0, stage_order.index(s))
                except ValueError:
                    return (1, s)

            product_total_seconds = 0

            for stage in sorted(stages.keys(), key=stage_sort_key):
                data = stages[stage]
                total_seconds = data["total_seconds"]
                count = data["count"]
                avg_seconds = total_seconds / count if count else 0

                rows.append(
                    {
                        "product_name": product_name,
                        "stage": stage,
                        "total_hours": round(total_seconds / 3600, 2),
                        "total_minutes": round(total_seconds / 60, 1),
                        "order_count": count,
                        "avg_minutes": round(avg_seconds / 60, 1),
                    }
                )
                product_total_seconds += total_seconds

            # Product total row — use distinct ticket count
            rows.append(
                {
                    "product_name": product_name,
                    "stage": None,  # signals a total row
                    "total_hours": round(product_total_seconds / 3600, 2),
                    "total_minutes": round(product_total_seconds / 60, 1),
                    "order_count": distinct_count,
                    "avg_minutes": round(
                        (product_total_seconds / distinct_count / 60)
                        if distinct_count
                        else 0,
                        1,
                    ),
                }
            )

        return rows

    def _get_filter_context(self, request):
        """Build choices for the filter form."""
        from company.models import Company

        companies = Company.objects.all().order_by("name")
        products = ProductTemplate.objects.all().order_by("name")

        if not request.user.is_superuser:
            employee = (
                Employee.objects.filter(user=request.user, is_active=True)
                .select_related("company")
                .first()
            )
            if employee and employee.company:
                companies = companies.filter(id=employee.company_id)
                products = products.filter(company=employee.company)

        return {
            "companies": companies,
            "products": products,
            "selected_company": request.GET.get("company", ""),
            "selected_product": request.GET.get("product", ""),
            "date_from": request.GET.get("date_from", ""),
            "date_to": request.GET.get("date_to", ""),
        }

    def get(self, request, *args, **kwargs):
        export = request.GET.get("export")

        sessions_qs = self._get_sessions_qs(request)
        rows = self._build_report_data(sessions_qs)

        if export == "xlsx":
            return self._export_xlsx(rows)

        filter_ctx = self._get_filter_context(request)

        context = {
            **self.model_admin.admin_site.each_context(request),
            "title": _("Product stage time report"),
            "rows": rows,
            "total_orders": max(
                (r["order_count"] for r in rows if r["stage"] is not None),
                default=0,
            ),
            "total_hours": round(
                sum(r["total_hours"] for r in rows if r["stage"] is not None), 2
            ),
            **filter_ctx,
        }
        return TemplateResponse(request, self.template_name, context)

    def _export_xlsx(self, rows):
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = str(_("Product Stage Report"))

        # Header style
        header_font = Font(bold=True, color="FFFFFF", size=11)
        header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
        header_alignment = Alignment(horizontal="center", vertical="center")
        thin_border = Border(
            bottom=Side(style="thin", color="D9D9D9"),
        )

        # Total row style
        total_font = Font(bold=True, size=11)
        total_fill = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid")

        headers = [
            str(_("Product")),
            str(_("Stage")),
            str(_("Total hours")),
            str(_("Total minutes")),
            str(_("Orders")),
            str(_("Avg minutes/order")),
        ]

        for col, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=header)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_alignment

        for row_idx, row in enumerate(rows, 2):
            is_total = row["stage"] is None

            ws.cell(row=row_idx, column=1, value=row["product_name"])
            ws.cell(row=row_idx, column=2, value=str(_("TOTAL")) if is_total else row["stage"])
            ws.cell(row=row_idx, column=3, value=row["total_hours"])
            ws.cell(row=row_idx, column=4, value=row["total_minutes"])
            ws.cell(row=row_idx, column=5, value=row["order_count"])
            ws.cell(row=row_idx, column=6, value=row["avg_minutes"])

            if is_total:
                for col in range(1, 7):
                    cell = ws.cell(row=row_idx, column=col)
                    cell.font = total_font
                    cell.fill = total_fill

        # Auto-width
        for col in range(1, 7):
            max_len = max(
                len(str(ws.cell(row=r, column=col).value or ""))
                for r in range(1, ws.max_row + 1)
            )
            ws.column_dimensions[chr(64 + col)].width = max(max_len + 4, 14)

        response = HttpResponse(
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        response["Content-Disposition"] = 'attachment; filename="product_stage_report.xlsx"'
        wb.save(response)
        return response
