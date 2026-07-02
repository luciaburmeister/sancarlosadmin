"""
Mixin that applies native Excel cell formatting when exporting to XLSX.

Usage:
    1. Define ``COLUMN_FORMATS`` on your ``ModelResource``::

        class MyResource(resources.ModelResource):
            COLUMN_FORMATS = {
                "amount_field": "#,##0.00",
                "pct_field":    "0.0%",
            }

    2. Mix ``FormattedXlsxExportMixin`` into your admin **before**
       ``ImportExportModelAdmin``::

        class MyAdmin(FormattedXlsxExportMixin, ImportExportModelAdmin, ModelAdmin):
            resource_classes = [MyResource]
"""

from io import BytesIO


class FormattedXlsxExportMixin:
    """
    Admin mixin that post-processes XLSX exports to apply openpyxl
    number formats declared in ``resource.COLUMN_FORMATS``.
    """

    def get_export_data(self, file_format, *args, **kwargs):
        data = super().get_export_data(file_format, *args, **kwargs)

        if file_format.get_extension() != "xlsx":
            return data

        from openpyxl import load_workbook

        wb = load_workbook(BytesIO(data))
        ws = wb.active

        resource_cls = self.resource_classes[0] if self.resource_classes else None
        if not resource_cls:
            return data

        resource = resource_cls()
        col_formats = getattr(resource, "COLUMN_FORMATS", {})
        if not col_formats:
            return data

        # Map header text → column index
        header_map = {cell.value: cell.column for cell in ws[1]}

        # Map header text → format code via resource fields
        header_to_fmt = {}
        for attr_name, fmt in col_formats.items():
            field = resource.fields.get(attr_name)
            if field and field.column_name in header_map:
                header_to_fmt[field.column_name] = fmt

        # Apply formatting to each data cell
        for col_name, fmt in header_to_fmt.items():
            col_idx = header_map[col_name]
            for row in ws.iter_rows(min_row=2, min_col=col_idx, max_col=col_idx):
                for cell in row:
                    if cell.value is not None:
                        cell.number_format = fmt

        out = BytesIO()
        wb.save(out)
        return out.getvalue()
