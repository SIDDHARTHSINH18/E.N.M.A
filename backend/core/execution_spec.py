"""
GHOST — ExecutionSpec (M4 step 2).

The resolved, immutable contract between planning and
execution:

    Planner -> PlanningResult --build_execution_spec()-->
    ExecutionSpec -> TaskSteps -> AutomationEngine

It exists so the translation that used to live inline in
the task API (PlanningResult -> TaskStep[]) becomes an
explicit, auditable, testable artifact. Nothing here plans
and nothing here executes:

- The Planner (backend/core/planner.py) is unchanged; a
  PlanningResult is consumed exactly as produced.
- Steps carrying no tool name stay advisory and are DROPPED
  here, preserving the existing rule that tool-less steps are
  never executed.
- The risk model is not redefined: risk levels come from the
  one existing RiskLevel enum in backend/tools/registry.py,
  and enforcement still happens later in PermissionPolicy.
  ExecutionSpec reports intent; it never authorizes anything.

Unknown tools are counted as DANGEROUS for the purposes of
this summary, mirroring PermissionPolicy's fail-closed rule
(unknown tool -> DENY), so a spec can never look safer than
the decision the gate will actually make.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backend.core.planner import PlanningResult
from backend.tools.registry import RiskLevel, ToolRegistry


# Deterministic severity order for summarizing a spec.
_RISK_ORDER = {
    RiskLevel.SAFE: 0,
    RiskLevel.SENSITIVE: 1,
    RiskLevel.DANGEROUS: 2,
}

# A tool named by the planner but absent from the registry is
# fail-closed: PermissionPolicy will deny it, so the spec must
# not describe it as safe.
_UNKNOWN_TOOL_RISK = RiskLevel.DANGEROUS


@dataclass(frozen=True)
class SpecStep:
    """
    One executable unit of an ExecutionSpec.

    Deliberately close to the existing automation TaskStep so
    the mapping is a straight pass-through.
    """

    order: int
    description: str
    tool: str
    params: Dict[str, Any] = field(default_factory=dict)
    # Orders (into this spec's executable steps) of steps that
    # must complete first. Always strictly earlier than ``order``
    # — advisory (tool-less) planned steps are dropped here, so
    # planned indices are remapped onto the dense executable
    # numbering, and references to dropped steps are discarded.
    depends_on: Tuple[int, ...] = ()

    def to_dict(self) -> dict:
        return {
            "order": self.order,
            "description": self.description,
            "tool": self.tool,
            "params": dict(self.params),
            "depends_on": list(self.depends_on),
        }


@dataclass(frozen=True)
class ExecutionSpec:
    """
    Immutable plan-of-record for one task.

    ``to_dict()`` is deterministic and JSON-safe so the spec
    can be written straight into an audit row.
    """

    task_id: str
    request: str
    planning_source: str
    steps: Tuple[SpecStep, ...] = ()
    skill: Optional[str] = None
    skill_risk_level: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    context_refs: Tuple[str, ...] = ()
    assumptions: Tuple[str, ...] = ()
    risk_level: str = RiskLevel.SAFE.value
    approval_required: bool = False
    expected_output: Optional[str] = None
    created_at: str = ""

    def __post_init__(self):
        for name in ("task_id", "request"):
            value = getattr(self, name)

            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"ExecutionSpec.{name} is required and "
                    "cannot be empty."
                )

        if not isinstance(self.planning_source, str) or not (
            self.planning_source.strip()
        ):
            raise ValueError(
                "ExecutionSpec.planning_source is required and "
                "cannot be empty."
            )

        object.__setattr__(
            self,
            "task_id",
            self.task_id.strip(),
        )
        object.__setattr__(
            self,
            "request",
            self.request.strip(),
        )

    @property
    def step_count(self) -> int:
        return len(self.steps)

    @property
    def has_executable_steps(self) -> bool:
        return bool(self.steps)

    def to_dict(self) -> dict:
        """JSON-safe, stable-order view of this spec."""

        return {
            "task_id": self.task_id,
            "request": self.request,
            "planning_source": self.planning_source,
            "steps": [step.to_dict() for step in self.steps],
            "skill": self.skill,
            "skill_risk_level": self.skill_risk_level,
            "provider": self.provider,
            "model": self.model,
            "context_refs": list(self.context_refs),
            "assumptions": list(self.assumptions),
            "risk_level": self.risk_level,
            "approval_required": self.approval_required,
            "expected_output": self.expected_output,
            "created_at": self.created_at,
        }


def build_execution_spec(
    planning: PlanningResult,
    task_id: str,
    registry: Optional[ToolRegistry] = None,
    skill_name: Optional[str] = None,
    skill_risk_level: Optional[RiskLevel | str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    memory_context: Optional[str] = None,
    document_context: Optional[str] = None,
    document_id: Optional[str] = None,
    expected_output: Optional[str] = None,
    created_at: str = "",
) -> ExecutionSpec:
    """
    Turn one PlanningResult (plus the skill the router chose)
    into the contract the executor will run.

    ``registry`` is the one shared production ToolRegistry; it
    is consulted only to summarize risk. Passing None keeps the
    steps but leaves risk at the fail-closed default, since
    without a registry nothing can be proven safe.
    """

    if planning is None:
        raise ValueError("build_execution_spec requires a PlanningResult.")

    if not planning.ready:
        raise ValueError(
            "Cannot build an ExecutionSpec from an unready planning "
            "result: the planner returned clarification questions."
        )

    steps: List[SpecStep] = []
    risks: List[RiskLevel] = []

    # Planned indices -> dense executable orders. Advisory
    # (tool-less) planned steps are dropped and get no mapping,
    # so a dependency that points at one is discarded.
    order_of_planned: Dict[int, int] = {}

    for planned_index, planned in enumerate(planning.steps):
        # Tool-less steps are advisory proposals: never executed.
        if not planned.tool or not str(planned.tool).strip():
            continue

        tool_name = str(planned.tool).strip()

        risk = _tool_risk(registry, tool_name)
        risks.append(risk)

        order = len(steps)

        order_of_planned[planned_index] = order

        steps.append(
            SpecStep(
                # Sequential renumbering: AutomationEngine orders
                # by this value, and dropped advisory steps must
                # not leave gaps.
                order=order,
                description=(
                    planned.description or f"Execute {tool_name}"
                ).strip(),
                tool=tool_name,
                params=dict(planned.params or {}),
                depends_on=tuple(
                    sorted(
                        {
                            order_of_planned[dep]
                            for dep in planned.depends_on
                            if dep in order_of_planned
                            and order_of_planned[dep] < order
                        }
                    )
                ),
            )
        )

    highest = _highest_risk(risks)

    context_refs = _context_refs(
        memory_context=memory_context,
        document_context=document_context,
        document_id=document_id,
    )

    return ExecutionSpec(
        task_id=task_id,
        request=planning.request or planning.task_description or "",
        planning_source=_source_value(planning),
        steps=tuple(steps),
        skill=skill_name,
        skill_risk_level=_risk_value(skill_risk_level),
        provider=provider,
        model=model,
        context_refs=context_refs,
        assumptions=tuple(planning.assumptions or ()),
        risk_level=highest.value,
        approval_required=highest == RiskLevel.SENSITIVE,
        expected_output=expected_output,
        created_at=created_at,
    )


# --------------------------------------------------------
# HELPERS
# --------------------------------------------------------

def _risk_value(risk: Optional[RiskLevel | str]) -> Optional[str]:
    """
    Normalize a declared risk level to its string form.

    SkillMetadata carries a RiskLevel enum while the routing
    layer hands over its ``.value``; the spec stores text, so
    both are accepted rather than forcing one caller shape.
    """

    if risk is None or risk == "":
        return None

    if isinstance(risk, RiskLevel):
        return risk.value

    text = str(risk).strip()

    return text or None


def _tool_risk(
    registry: Optional[ToolRegistry],
    tool_name: str,
) -> RiskLevel:
    if registry is None:
        return _UNKNOWN_TOOL_RISK

    try:
        return registry.get_tool(tool_name).risk_level
    except KeyError:
        return _UNKNOWN_TOOL_RISK


def _highest_risk(risks: Sequence[RiskLevel]) -> RiskLevel:
    if not risks:
        # No executable step (a pure model/skill task, or an
        # empty plan) performs no tool action at all.
        return RiskLevel.SAFE

    return max(risks, key=lambda risk: _RISK_ORDER[risk])


def _source_value(planning: PlanningResult) -> str:
    source = getattr(planning, "source", None)

    if source is None:
        return "UNKNOWN"

    return str(getattr(source, "value", source))


def _context_refs(
    memory_context: Optional[str],
    document_context: Optional[str],
    document_id: Optional[str],
) -> Tuple[str, ...]:
    """
    Record only references, never content: the context strings
    can carry document text or retrieved memory, which must not
    be duplicated into the spec or the audit log.
    """

    refs: List[str] = []

    if memory_context and memory_context.strip():
        refs.append("memory")

    if document_context and document_context.strip():
        refs.append("document-context")

    if document_id and str(document_id).strip():
        refs.append(f"document:{str(document_id).strip()}")

    return tuple(refs)
