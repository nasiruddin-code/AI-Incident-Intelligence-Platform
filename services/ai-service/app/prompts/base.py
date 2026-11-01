"""
Minimal reusable prompt-templating framework.

Deliberately simple (no external templating dependency) — the point is a
consistent pattern (system instruction + user template + `.render(**kw)`)
that later weeks reuse for RCA prompts, runbook prompts, and agent tool
prompts, instead of every feature hand-rolling f-strings inline.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class PromptTemplate:
    name: str
    system_instruction: str
    template: str

    def render(self, **kwargs: str) -> str:
        try:
            return self.template.format(**kwargs)
        except KeyError as exc:
            raise ValueError(
                f"Prompt '{self.name}' is missing required variable {exc}"
            ) from exc
