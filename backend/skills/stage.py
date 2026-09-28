"""
GHOST — skill stage (M4 step 5).

Wires the existing skill system into the task pipeline without
adding a second one:

  requirement -> SkillRouter.select() (exactly once)
              -> SkillSelection (recorded in the ExecutionSpec
                 and the audit log)
              -> SkillRunner.execute() (only for a skill that
                 is both matched and runnable)

Two rules this layer adds on top of the existing pieces:

- ROUTE ONCE. The router is consulted a single time per
  request, and the ranked candidates it returned are reused
  for selection, spec, audit and execution. The pipeline
  cannot disagree with itself about what was chosen.
- RUNNABLE BEFORE RUN. A skill executes only when every tool
  in its required_tools is present in the ToolRegistry. That
  is metadata, so the decision happens before any skill
  implementation is imported. A matched-but-unrunnable skill
  is reported the same way as "nothing matched" — router.py
  already defines no-match as a safe fallback to the plain
  task path, not an error.

Execution still goes through SkillRunner -> Agent ->
PermissionPolicy: this module never runs a tool and never
decides a permission.
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple

from backend.core.task import Task
from backend.skills.registry import SkillRegistry
from backend.skills.router import SkillRouter
from backend.skills.runner import SkillRunner
from backend.skills.skill import SkillResult
from backend.tools.registry import ToolRegistry


DEFAULT_CANDIDATE_LIMIT = 5

# Conventional input keys the builtin skills read their main
# requirement from ("goal", "text", "query"). The requirement is
# offered under each of them — a skill reads the one it wants and
# ignores the rest — so no skill name is hardcoded here.
INPUT_PARAM_KEYS = ("goal", "text", "query", "prompt")


@dataclass(frozen=True)
class SkillSelection:
    """
    What one routing round decided.

    ``skill`` is None for the no-match outcome, which is normal:
    it means "run the plain task path", not "failure".
    """

    skill: Optional[str] = None
    reason: str = ""
    candidates: Tuple[str, ...] = ()
    unavailable: Tuple[str, ...] = ()
    risk_level: Optional[str] = None

    @property
    def matched(self) -> bool:
        return self.skill is not None

    def to_dict(self) -> dict:
        """Audit/spec view. Keys are stable."""

        return {
            "skill": self.skill,
            "matched": self.matched,
            "risk_level": self.risk_level,
            "reason": self.reason,
            "candidates": list(self.candidates),
            "unavailable": list(self.unavailable),
        }


class SkillStage:
    """Routing + execution seam over the existing skill system."""

    def __init__(
        self,
        registry: SkillRegistry,
        router: SkillRouter,
        runner: SkillRunner,
        tool_registry: ToolRegistry,
        candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    ):
        self._registry = registry
        self._router = router
        self._runner = runner
        self._tools = tool_registry
        self._candidate_limit = candidate_limit

    # --------------------------------------------------------
    # ROUTING (one router call)
    # --------------------------------------------------------

    def missing_tools(self, skill_name: str) -> List[str]:
        """Required tools this registry does not provide."""

        metadata = self._registry.get_metadata(skill_name)

        available = {tool.name for tool in self._tools.list_tools()}

        return [
            name
            for name in metadata.required_tools
            if name not in available
        ]

    def select(self, requirement: str) -> SkillSelection:
        """
        Rank skills for the requirement and pick the best one
        that can actually run.
        """

        ranked = self._router.select(
            requirement,
            limit=self._candidate_limit,
        )

        candidates = tuple(metadata.name for metadata in ranked)

        if not ranked:
            return SkillSelection(
                reason="no skill matched the requirement",
                candidates=candidates,
            )

        unavailable: List[str] = []

        for metadata in ranked:
            missing = self.missing_tools(metadata.name)

            if missing:
                unavailable.append(metadata.name)
                continue

            return SkillSelection(
                skill=metadata.name,
                reason=(
                    f"{metadata.name} is the highest-ranked "
                    "match that can run with the registered tools"
                ),
                candidates=candidates,
                unavailable=tuple(unavailable),
                risk_level=metadata.risk_level.value,
            )

        # Every candidate needs a tool GHOST does not have.
        return SkillSelection(
            reason=(
                "matched skills require unregistered tools: "
                + ", ".join(
                    f"{name} ({', '.join(self.missing_tools(name))})"
                    for name in unavailable
                )
            ),
            candidates=candidates,
            unavailable=tuple(unavailable),
        )

    # --------------------------------------------------------
    # EXECUTION
    # --------------------------------------------------------

    def build_params(
        self,
        requirement: str,
        params: Optional[dict] = None,
    ) -> dict:
        """
        Offer the requirement to the skill under each
        conventional input key. An explicit params mapping
        always wins.
        """

        built = dict(params or {})

        for key in INPUT_PARAM_KEYS:
            built.setdefault(key, requirement)

        return built

    def execute(
        self,
        selection: SkillSelection,
        task: Task,
        params: Optional[dict] = None,
    ) -> SkillResult:
        """
        Run the selected skill through the shared SkillRunner.

        Callers must check ``selection.matched`` first: the
        no-match outcome means "use the plain task path".
        """

        if not selection.matched:
            raise ValueError(
                "Cannot execute a skill selection that matched "
                f"nothing ({selection.reason})."
            )

        return self._runner.execute(
            selection.skill,
            task,
            self.build_params(task.description, params),
        )

    async def execute_async(
        self,
        selection: SkillSelection,
        task: Task,
        params: Optional[dict] = None,
    ) -> SkillResult:
        """
        Async twin of execute(): same gate, same shared runner,
        but skills implemented as coroutines (async-tool skills
        such as model-backed summarization) are awaited instead
        of refused.
        """

        if not selection.matched:
            raise ValueError(
                "Cannot execute a skill selection that matched "
                f"nothing ({selection.reason})."
            )

        return await self._runner.execute_async(
            selection.skill,
            task,
            self.build_params(task.description, params),
        )
