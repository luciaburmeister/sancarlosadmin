"""
Prompts a user pastes into their own AI assistant, together with an export
from their previous system, to get back a file our importers accept.

A step builds one per file it imports. Everything a prompt says about a column
must match what the importer actually reads, so build it next to the importer's
own column names rather than copying them.
"""
from dataclasses import dataclass, field

from django.utils.translation import gettext as _


@dataclass(frozen=True)
class Column:
    header: str
    meaning: str
    required: bool = False


@dataclass
class ImportPrompt:
    title: str
    # What one row of the file is, e.g. "one row per client"
    rows: str
    columns: list[Column]
    rules: list[str] = field(default_factory=list)

    @property
    def text(self):
        lines = [
            _(
                "I'm moving to a new ERP and need to import data from my previous system. I'm attaching "
                "what I exported from it. Turn it into an Excel file (.xlsx) the new ERP can import: "
                "a single sheet, these headers in the first row written exactly as shown, and %(rows)s."
            ) % {"rows": self.rows},
            "",
            _("Columns:"),
        ]
        for column in self.columns:
            required = _(" (required)") if column.required else ""
            lines.append(f'- "{column.header}"{required}: {column.meaning}')
        lines += ["", _("Rules:")]
        lines += [f"- {rule}" for rule in [
            *self.rules,
            _("Leave out any column not listed above, and don't add columns of your own."),
            _("Write amounts as plain numbers with a dot for decimals and no currency symbol or "
              "thousands separator, e.g. 1234.50."),
            _("Never invent data: leave a cell empty when the export doesn't have it. Leave out rows "
              "missing a required column."),
            _("When you're done, give me the .xlsx to download, and list the rows you left out or "
              "weren't sure about, saying why."),
        ]]
        return "\n".join(lines)
