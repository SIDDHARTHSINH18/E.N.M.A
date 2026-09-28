"""
GHOST — production M3 service wiring (M3-H step 1).

Builds the shared runtime objects for the agent pipeline
using the existing M3 implementations exactly as they are
(no M3-A..M3-G file is modified):

  ToolRegistry -> PermissionPolicy -> production executor
    -> Agent -> AutomationEngine
  plus SkillRegistry/SkillLoader/SkillRouter/SkillRunner
  and ReflectionEngine singletons.

Import direction is strictly one-way:
  agent_services -> tools/permissions/agents/automation/
  skills/reflection. Nothing below imports this module
  back (no circular imports), and nothing exposes these
  services through an API yet (no main.py change).

There is exactly one model gateway (core.orchestrator)
and this module creates none.
"""

import os

from backend.agents.executor import Agent
from backend.agents.memory_bridge import MemoryBridge
from backend.approval.service import ApprovalService
from backend.automation.engine import AutomationEngine
from backend.audit.log import AuditLog
from backend.core.planner import Planner
from backend.core.services import memory_service, orchestrator
from backend.permissions.policy import PermissionPolicy
from backend.reflection.engine import ReflectionEngine
from backend.skills.builtin import register_builtin_skills
from backend.skills.loader import SkillLoader
from backend.skills.registry import SkillRegistry
from backend.skills.router import SkillRouter
from backend.skills.runner import SkillRunner
from backend.skills.stage import SkillStage
from backend.tasks import TaskService
from backend.tasks.runner import TaskRunner
from backend.tools.builtin.fs import (
    fs_file_exists,
    fs_list_directory,
    fs_read_file,
    fs_write_file,
)
from backend.tools.builtin.memory_tools import memory_search
from backend.tools.builtin.model import model_generate
from backend.tools.builtin.summarize import summarize
from backend.tools.builtin.web_fetch import web_fetch
from backend.tools.builtin.web_search import web_search
from backend.tools.registry import RiskLevel, ToolRegistry


# ============================================================
# TOOL REGISTRY (production)
# ============================================================

tool_registry = ToolRegistry()


def register_builtin_tools(registry: ToolRegistry) -> None:
    """Register every real builtin tool with explicit
    category and risk level. New tools are added here."""

    registry.register(
        name="fs_read_file",
        description=(
            "Read a local text file (absolute path, "
            "max 1 MB, read-only)."
        ),
        category="filesystem",
        risk_level=RiskLevel.SAFE,
    )
    registry.register(
        name="fs_list_directory",
        description=(
            "List immediate entries in a local directory "
            "(absolute path, read-only)."
        ),
        category="filesystem",
        risk_level=RiskLevel.SAFE,
    )
    registry.register(
        name="fs_file_exists",
        description=(
            "Check whether a local regular file exists "
            "(absolute path, read-only)."
        ),
        category="filesystem",
        risk_level=RiskLevel.SAFE,
    )
    registry.register(
        name="model_generate",
        description=(
            "Ask the ENMA model for one text answer through "
            "the shared model gateway (read-only; no files, "
            "no side effects)."
        ),
        category="model",
        risk_level=RiskLevel.SAFE,
    )
    registry.register(
        name="memory_search",
        description=(
            "Search the user's persistent memory store and "
            "return the best matching records (read-only)."
        ),
        category="memory",
        risk_level=RiskLevel.SAFE,
    )
    registry.register(
        name="summarize",
        description=(
            "Condense one text into a shorter factual summary "
            "through the model gateway (read-only)."
        ),
        category="model",
        risk_level=RiskLevel.SAFE,
    )
    registry.register(
        name="fs_write_file",
        description=(
            "Write UTF-8 text content into one file inside the "
            "sandboxed workspace root (requires user approval)."
        ),
        category="filesystem",
        risk_level=RiskLevel.SENSITIVE,
    )
    registry.register(
        name="web_search",
        description=(
            "Search the public web through the configured "
            "search provider and return result leads (read-"
            "only; requires a provider to be configured)."
        ),
        category="web",
        risk_level=RiskLevel.SAFE,
    )
    registry.register(
        name="web_fetch",
        description=(
            "Retrieve one public HTTP(S) page with SSRF, "
            "size, time and redirect limits, and extract its "
            "readable text (read-only)."
        ),
        category="web",
        risk_level=RiskLevel.SAFE,
    )


register_builtin_tools(tool_registry)


# ============================================================
# PRODUCTION TOOL EXECUTOR
# ============================================================
#
# The ONLY bridge from the Agent to real tool code.
# Defensive by design:
# - dispatch table is a fixed, code-defined mapping
# - a tool name must exist in the ToolRegistry AND in
#   the table before anything runs
# - unknown/unregistered names raise (the Agent maps
#   the failure to a FAILED task; the PermissionPolicy
#   has already denied them before this point anyway)
# - an async implementation (model_generate) returns a
#   coroutine: only Agent.execute_async() awaits it, so the
#   synchronous path can never silently drop a model call

TOOL_IMPLEMENTATIONS = {
    "fs_read_file": fs_read_file,
    "fs_list_directory": fs_list_directory,
    "fs_file_exists": fs_file_exists,
    "fs_write_file": fs_write_file,
    "model_generate": model_generate,
    "memory_search": memory_search,
    "summarize": summarize,
    "web_search": web_search,
    "web_fetch": web_fetch,
}


def production_tool_executor(tool_name: str, params: dict):
    """
    Execute one registered builtin tool. Params are
    untrusted: each tool validates its own inputs.
    """

    try:
        tool_registry.get_tool(tool_name)
    except KeyError:
        raise KeyError(
            f"Tool '{tool_name}' is not registered."
        )

    implementation = TOOL_IMPLEMENTATIONS.get(tool_name)

    if implementation is None:
        raise KeyError(
            f"Tool '{tool_name}' is registered but has "
            "no implementation wired."
        )

    return implementation(params if isinstance(params, dict) else {})


# ============================================================
# SINGLETONS
# ============================================================

# Explicit approval records (M3-H step 5). Shared by the
# policy (one-shot grant consumption) and the API layer
# (decision endpoints), so there is exactly one approval
# store.
approval_service = ApprovalService()

permission_policy = PermissionPolicy(
    tool_registry,
    approval_grants=approval_service,
)

agent = Agent(
    name="ghost-agent",
    registry=tool_registry,
    permission_policy=permission_policy,
    tool_executor=production_tool_executor,
)

automation_engine = AutomationEngine(agent)

skill_registry = SkillRegistry()
register_builtin_skills(skill_registry)

skill_loader = SkillLoader(skill_registry)
skill_router = SkillRouter(skill_registry)

skill_runner = SkillRunner(
    agent=agent,
    skill_registry=skill_registry,
    loader=skill_loader,
    permission_policy=permission_policy,
    automation_engine=automation_engine,
)

# M4 step 5: the routing/execution seam that lets the task
# pipeline use the skill system above. It holds references to
# the existing singletons — it is not another registry,
# router or runner.
skill_stage = SkillStage(
    registry=skill_registry,
    router=skill_router,
    runner=skill_runner,
    tool_registry=tool_registry,
)

reflection_engine = ReflectionEngine()


# ============================================================
# AUDIT + MEMORY (M4 steps 1 and 6)
# ============================================================

# The single append-only audit store. GHOST_AUDIT_PATH redirects
# it in tests, exactly like GHOST_MEMORY_PATH for memory.
audit_log = AuditLog()

memory_bridge = MemoryBridge(
    memory=memory_service,
    audit=audit_log,
)

# MODEL-stage audit events: the gateway reports every provider
# call (provider/model/ok/duration/error type — never secrets)
# into the same single audit store. A raising hook is guarded
# inside the gateway; auditing can never fail a model call.
orchestrator.audit_hook = audit_log.append


# ============================================================
# PLANNING + TASK STORE (M4 step 9)
# ============================================================
#
# One task store and one runner for the process, assembled from
# the singletons above. The HTTP layer imports these and names
# their stage order; it does not build its own collaborators.

# TEMPORARY (M3 testing): the planner points at the Groq
# provider because Gemini generation is currently quota-exhausted
# (HTTP 429 RESOURCE_EXHAUSTED) while the same Groq provider and
# model serve chat successfully. Purely a provider selection on
# the existing gateway — no new client, no M3 logic touched.
# Groq requires an explicit model on every request, so the
# configured GROQ_MODEL is passed through; other providers keep
# the gateway default (model=None).
# Reversible without a code change: set ENMA_PLANNER_PROVIDER=gemini
# (or remove the override once Gemini quota is available again).
planner_provider = os.getenv("ENMA_PLANNER_PROVIDER", "groq")

planner = Planner(
    orchestrator,
    provider_name=planner_provider,
    model=(
        os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
        if planner_provider == "groq"
        else None
    ),
    tool_catalog=[
        tool.name for tool in tool_registry.list_tools()
    ],
)

# Task history persists across restarts (M4). The path comes
# from GHOST_TASKS_PATH (packaged desktop runtime redirects it
# into the user's data directory, like memory/audit).
task_service = TaskService(persistent=True)

# Bounded automatic recovery is enabled only for explicitly
# SAFE tools: re-running them cannot write anything or surface
# an approval decision again. SENSITIVE/DANGEROUS tools never
# auto-retry — a paused or denied workflow keeps its exact
# human decision.
_retryable_tools = frozenset(
    tool.name
    for tool in tool_registry.list_tools()
    if tool.risk_level is RiskLevel.SAFE
)

task_runner = TaskRunner(
    task_service=task_service,
    automation_engine=automation_engine,
    approvals=approval_service,
    reflection_engine=reflection_engine,
    retryable_tools=_retryable_tools,
)
