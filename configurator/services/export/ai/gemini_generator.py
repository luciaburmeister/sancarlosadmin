"""
Gemini adapter for export template AI generation.

Only file in this feature that talks to google.genai directly. Takes already-assembled
strings (prompt, context, previous html/css) and returns generated html/css - it never
fetches context or template state itself; that's the orchestrator's job (ai_service.py).
"""

import json
import google.genai as genai
from google.genai import types

from .interface import TemplateAIInterface, TemplateGenerationResult


class GeminiTemplateGenerator(TemplateAIInterface):

    MODEL_NAME = "gemini-3-flash-preview"

    SCHEMA = {
        "type": "OBJECT",
        "required": ["html", "css"],
        "properties": {
            "html": {"type": "STRING", "description": "The full Django-template HTML for the document body."},
            "css": {"type": "STRING", "description": "The CSS, without a surrounding <style> tag."},
        },
    }

    def __init__(self, api_key: str):
        self.client = genai.Client(api_key=api_key)

    def generate(
        self,
        *,
        prompt: str,
        kind: str,
        context_json: str,
        previous_html: str,
        previous_css: str,
    ) -> TemplateGenerationResult:
        full_prompt = f"""
You are editing an HTML/CSS export template for a "{kind}" document in an ERP system.

The html is rendered through Django's template engine, then converted to an A4 PDF via
WeasyPrint. The output is PAPER, never a browser viewport. Design for a fixed-width page.

PRINT RULES (mandatory):
- A shared base stylesheet already provides: @page (A4, 12-14mm margins, page numbers),
  the 'Inter' @font-face, box-sizing, repeating <thead> across pages and page-break rules
  for table rows. Do NOT add @page, @import, @font-face or any web font URL yourself.
- Do NOT wrap the document in a centred container with max-width, "margin: auto" or large
  padding. The page margins already exist. The outer element should be width: 100%.
- Use mm and pt units for sizes and spacing. Never use vw, vh, rem or media queries; there is
  no viewport and @media screen rules are ignored.
- Anything with columns (line items, totals, side-by-side address blocks, header with logo
  left and title right) must be a <table> or floats with explicit percentage widths.
  Flexbox is allowed only for simple single-row alignment; never rely on flex-wrap.
  Do not use CSS Grid.
- Percentages are relative to the page content width, so column widths in a table should
  add up to 100%.
- Mark blocks that must not split across pages (totals, signatures, address blocks) with
  page-break-inside: avoid. Long tables may span pages; their header repeats automatically.
- Images: set an explicit max-height in mm for logos; never let an image exceed 100% width.
- No JavaScript (it is never executed), no external resources except image URLs provided
  by the data variables.

AVAILABLE DATA VARIABLES (JSON - these are SAMPLE values from one record, shown only so you
understand the shape of the data):
{context_json}

Rules:
- Reference these variables in the html using Django template syntax, e.g. {{{{ variable_name }}}}.
- Do NOT invent variable names that aren't in the JSON above.
- Do NOT hardcode the sample values above as literal text in the html - they must stay as
  {{{{ variable_name }}}} placeholders so the template works for every record, not just this one.

CURRENT HTML:
{previous_html}

CURRENT CSS:
{previous_css}

USER INSTRUCTION:
{prompt}

Apply the user's instruction. Preserve the existing structure, styling, and branding
(colors, fonts, layout) except where the instruction asks you to change it.
Return the complete html and css, not just a diff or the changed fragment.
"""

        response = self.client.models.generate_content(
            model=self.MODEL_NAME,
            contents=[full_prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=self.SCHEMA,
                temperature=0.2,
            ),
        )

        part = response.candidates[0].content.parts[0]
        raw = json.loads(part.json())["text"]
        data = json.loads(raw) if isinstance(raw, str) else raw

        return TemplateGenerationResult(html=data["html"], css=data["css"])
