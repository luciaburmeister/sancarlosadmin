"""
Export template AI generation interface - defines the contract for any AI provider.

This is a "contract" or "rulebook" NOT a workflow.
Any provider (Gemini, OpenAI, etc.) must implement this interface.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class TemplateGenerationResult:
    """HTML/CSS produced by an AI provider for an export template."""
    html: str
    css: str


class TemplateAIInterface(ABC):

    @abstractmethod
    def generate(
        self,
        *,
        prompt: str,
        kind: str,
        context_json: str,
        previous_html: str,
        previous_css: str,
    ) -> TemplateGenerationResult:
        """
        Generate (or revise) the html/css for an export template.

        Args:
            prompt: The user's natural-language instruction.
            kind: The ExportTemplateKind value (e.g. "sales_invoice").
            context_json: Serialized JSON of the variables available to the template.
            previous_html: The html currently in the editor (or a skeleton default).
            previous_css: The css currently in the editor (or a skeleton default).

        Returns:
            TemplateGenerationResult: the new html/css.
        """
        raise NotImplementedError("Subclasses must implement generate method")
