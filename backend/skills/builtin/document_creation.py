"""
GHOST builtin skill: document-creation (M4).

Generates document content with the model (model_generate,
SAFE) and writes it to a file inside the sandboxed workspace
(fs_write_file, SENSITIVE — the write step pauses for an
explicit user approval through the normal approval flow).

The skill only PLANS these steps and chains their outputs:
every execution still goes through Agent -> PermissionPolicy,
and nothing is claimed written that was not verified through
the tool's real return value.
"""

from datetime import datetime

from backend.agents.executor import Agent
from backend.automation.engine import AutomationEngine, TaskStep
from backend.core.task import Task, TaskStatus
from backend.skills.metadata import SkillMetadata
from backend.skills.skill import (
    Skill,
    SkillResult,
    status_from_workflow,
)
from backend.tools.registry import RiskLevel


class DocumentCreationSkill(Skill):

    metadata = SkillMetadata(
        name="document-creation",
        description=(
            "Generate a document from a prompt with the model "
            "and save it into the workspace (saving requires "
            "user approval)."
        ),
        category="documents",
        version="1.0",
        required_tools=["model_generate", "fs_write_file"],
        risk_level=RiskLevel.SENSITIVE,
        entrypoint=(
            "backend.skills.builtin.document_creation:DocumentCreationSkill"
        ),
    )

    async def run(
        self,
        task: Task,
        agent: Agent,
        params: dict,
    ) -> SkillResult:
        """
        Coroutine skill: chaining needs each step's real output
        before the next step's params exist, so each engine pass
        is awaited explicitly. Both passes re-enter
        Agent -> PermissionPolicy; the SENSITIVE write pauses
        the workflow exactly like any other approval.
        """

        prompt = str(params.get("prompt", "")).strip()
        path = str(params.get("path", "")).strip()

        if not prompt:
            return SkillResult(
                skill_name=self.metadata.name,
                status=TaskStatus.FAILED,
                error=(
                    "document-creation requires a 'prompt' "
                    "parameter."
                ),
            )

        if not path:
            # Real default inside the workspace root: a fresh
            # timestamped file per run, never an overwrite and
            # never a fabricated location.
            path = (
                "enma-documents/generated-"
                + datetime.now().strftime("%Y%m%d-%H%M%S")
                + ".md"
            )

        engine = params.get("automation_engine") or AutomationEngine(agent)

        generate_step = TaskStep(
            tool_name="model_generate",
            params={"prompt": prompt},
            order=0,
            title="Generate document content",
        )

        generation = await engine.run_async(task, [generate_step])

        if generation.state.value != "COMPLETED":
            return SkillResult(
                skill_name=self.metadata.name,
                status=status_from_workflow(generation.state),
                error=task.error,
                steps=[generate_step],
            )

        write_step = TaskStep(
            tool_name="fs_write_file",
            params={
                "path": path,
                "content": str(generate_step.result or ""),
            },
            order=1,
            title=f"Write document to {path}",
        )

        write = await engine.run_async(task, [write_step])

        return SkillResult(
            skill_name=self.metadata.name,
            status=status_from_workflow(write.state),
            output=write_step.result
            if write.state.value == "COMPLETED"
            else None,
            error=task.error
            if write.state.value == "FAILED"
            else None,
            steps=[generate_step, write_step],
        )
