"""
GHOST builtin skill: note-summarizer (M3-F).

Summarizes a text through the "summarize" tool (SAFE).
Planning only — execution and permission checking stay in
the Agent -> PermissionPolicy path.
"""

from backend.agents.executor import Agent
from backend.automation.engine import AutomationEngine, TaskStep
from backend.core.task import Task
from backend.skills.metadata import SkillMetadata
from backend.skills.skill import (
    Skill,
    SkillResult,
    status_from_workflow,
)
from backend.tools.registry import RiskLevel


class NoteSummarizerSkill(Skill):

    metadata = SkillMetadata(
        name="note-summarizer",
        description=(
            "Summarize a note or text into a shorter form "
            "using the summarize tool."
        ),
        category="documents",
        version="1.0",
        required_tools=["summarize"],
        risk_level=RiskLevel.SAFE,
        entrypoint=(
            "backend.skills.builtin.note_summarizer:NoteSummarizerSkill"
        ),
    )

    async def run(
        self,
        task: Task,
        agent: Agent,
        params: dict,
    ) -> SkillResult:
        """
        Async coroutine skill: the summarize tool reaches the
        model through the async gateway, so its engine pass
        must go through run_async (Agent.execute_async). The
        permission path is identical — each step re-enters
        Agent -> PermissionPolicy.
        """

        text = str(params.get("text", "")).strip()

        if not text:
            return SkillResult(
                skill_name=self.metadata.name,
                status=TaskStatus.FAILED,
                error="note-summarizer requires a 'text' parameter.",
            )

        engine = params.get("automation_engine") or AutomationEngine(agent)

        step = TaskStep(
            tool_name="summarize",
            params={"text": text},
            order=0,
            title="Summarize text",
        )

        outcome = await engine.run_async(task, [step])

        return SkillResult(
            skill_name=self.metadata.name,
            status=status_from_workflow(outcome.state),
            output=step.result if outcome.state.value == "COMPLETED" else None,
            error=task.error if outcome.state.value == "FAILED" else None,
            steps=[step],
        )
