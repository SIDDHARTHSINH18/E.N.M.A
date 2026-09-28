"""
GHOST — Planner (M3-H step 3).

Turns a user request into an explicit, auditable
planning result: either a ready task brief
(title / description / proposed steps) or one round of
clarification questions.

Hard rules enforced in CODE, not prompt wording:

- ABSOLUTE_MAX_QUESTIONS = 7 — a hard ceiling on the
  number of clarification questions in one round. Any
  model output containing more is truncated to the
  first 7 (highest-priority = model's own order) and
  the truncation is recorded. Nothing is invented for
  the dropped questions.
- The Planner NEVER executes anything: it holds no
  ToolRegistry, no PermissionPolicy, no executor and
  no skill components. Steps are inert proposals;
  the Agent + PermissionPolicy gate every real action
  later (M3-D), so nothing here can bypass policy.
- Model access goes ONLY through the existing
  orchestrator gateway (backend.core.orchestrator).
  This module creates no provider and no second
  orchestrator.
- Minimum necessary questions: when a usable plan is
  present, questions are treated as unnecessary and
  dropped (recorded); empty/duplicate questions are
  filtered; a clarification round never ends with zero
  questions (a safe fallback question is added).

Deterministic fallbacks keep planning auditable even
when the model is unavailable or returns malformed
output: the raw response is preserved verbatim and the
fallback path is labeled in the result.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, List, Optional


# Hard ceiling: 7 is the maximum, never the target.
ABSOLUTE_MAX_QUESTIONS = 7

# Heuristic floor for the deterministic (no-model) path:
# a request shorter than this is not actionable.
_MIN_REQUEST_WORDS = 3

_MAX_TITLE_CHARS = 80


class PlanningSource(Enum):
    """Where the planning result came from."""

    MODEL = "MODEL"
    DETERMINISTIC = "DETERMINISTIC"
    FALLBACK_MALFORMED = "FALLBACK_MALFORMED"
    FALLBACK_ERROR = "FALLBACK_ERROR"


@dataclass
class PlannedStep:
    """
    One proposed action. Inert data: the Planner never
    executes steps — tool names are advisory strings the
    executor (+ PermissionPolicy) will evaluate later.

    ``depends_on`` lists indices (into the steps list) of
    steps that must complete before this one may run. The
    planner may only reference STRICTLY EARLIER steps;
    the executor enforces that ordering anyway, fail-closed.
    """

    description: str
    tool: Optional[str] = None
    params: dict = field(default_factory=dict)
    depends_on: List[int] = field(default_factory=list)


@dataclass
class PlanningResult:
    """
    Explicit, auditable planning outcome.

    Exactly one of the two branches is populated:
    - ready=True    -> task_title/task_description (+ steps)
    - ready=False   -> questions (1..ABSOLUTE_MAX_QUESTIONS)
    """

    request: str
    ready: bool
    task_title: Optional[str] = None
    task_description: Optional[str] = None
    steps: List[PlannedStep] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    questions: List[str] = field(default_factory=list)
    questions_truncated: bool = False
    questions_dropped: int = 0
    source: PlanningSource = PlanningSource.DETERMINISTIC
    parse_ok: bool = True
    raw_response: Optional[str] = None
    planned_at: str = ""

    def to_dict(self) -> dict:
        """JSON-safe audit view of this planning result."""

        return {
            "request": self.request,
            "ready": self.ready,
            "task_title": self.task_title,
            "task_description": self.task_description,
            "steps": [
                {
                    "description": step.description,
                    "tool": step.tool,
                    "params": step.params,
                    "depends_on": list(step.depends_on),
                }
                for step in self.steps
            ],
            "assumptions": list(self.assumptions),
            "questions": list(self.questions),
            "questions_truncated": self.questions_truncated,
            "questions_dropped": self.questions_dropped,
            "source": self.source.value,
            "parse_ok": self.parse_ok,
            "raw_response": self.raw_response,
            "planned_at": self.planned_at,
        }


class Planner:
    """
    Deterministic-first planner on top of the existing
    model gateway.

    Usage:
        planner = Planner(orchestrator)          # model-assisted
        planner = Planner()                      # deterministic only
        result = await planner.plan("user request")
    """

    MAX_QUESTIONS_PER_ROUND = ABSOLUTE_MAX_QUESTIONS

    def __init__(
        self,
        orchestrator: Any = None,
        provider_name: Optional[str] = None,
        model: Optional[str] = None,
        tool_catalog: Optional[List[str]] = None,
    ):
        # Duck-typed: any object with an async generate()
        # matching the Orchestrator gateway signature.
        self._orchestrator = orchestrator
        self._provider_name = provider_name
        self._model = model
        # Registered tool names the model may reference in
        # planned steps. Advisory only: steps stay inert and
        # the PermissionPolicy still gates every real action.
        self._tool_catalog = list(tool_catalog or [])

    # ========================================================
    # PUBLIC API
    # ========================================================

    async def plan(
        self,
        request: str,
        memory_context: Optional[str] = None,
        document_context: Optional[str] = None,
        conversation_history: Optional[list] = None,
    ) -> PlanningResult:
        """
        Plan one user request. Never raises for bad model
        behavior: every failure mode degrades to a
        deterministic, auditable result.
        """

        planned_at = datetime.now().isoformat()

        request_text = (request or "").strip()

        if not request_text:
            return PlanningResult(
                request="",
                ready=False,
                questions=[
                    "What would you like GHOST to do?"
                ],
                source=PlanningSource.DETERMINISTIC,
                planned_at=planned_at,
            )

        if self._orchestrator is None:
            return self._deterministic_plan(
                request_text, planned_at
            )

        try:
            raw = await self._call_model(
                request_text,
                memory_context=memory_context,
                document_context=document_context,
                conversation_history=conversation_history,
            )
        except Exception as exc:
            # Gateway failure must not break planning: fall
            # back deterministically and record it (type only,
            # never the exception text, to avoid leaking
            # provider details into audit output).
            return self._fallback_gateway_error(
                request_text, planned_at, exc
            )

        return self._plan_from_model_output(
            request_text, raw, planned_at
        )

    # ========================================================
    # MODEL PATH
    # ========================================================

    async def _call_model(
        self,
        request_text: str,
        memory_context,
        document_context,
        conversation_history,
    ) -> str:
        """
        One gateway call. Reuses the existing Orchestrator
        (and through it, the configured provider). Raises
        only if the gateway itself fails; callers handle it.
        """

        message = (
            "You are the GHOST Planner. Decide whether the "
            "request below has enough information to turn "
            "into a task.\n\n"
            "Reply with ONLY a JSON object, no other text:\n"
            "{\n"
            '  "enough_information": true | false,\n'
            '  "task_title": string or null,\n'
            '  "task_description": string or null,\n'
            '  "steps": [{"description": string, '
            '"tool": string or null, "params": {}, '
            '"depends_on": [earlier step indices this step '
            "needs completed first]}],\n"
            '  "assumptions": [string],\n'
            '  "clarifying_questions": [string]\n'
            "}\n\n"
            "Rules:\n"
            "- Ask only the minimum necessary clarifying "
            "questions; 7 is an absolute maximum.\n"
            "- Prefer sensible defaults over questions "
            "when the request is actionable.\n"
            "- If you provide a plan, set "
            "enough_information to true.\n"
            "- Steps are proposals only; they are not "
            "executed here.\n"
        )

        if self._tool_catalog:
            message += (
                "\nAvailable tools (use exactly these "
                "names in steps; omit 'tool' for steps "
                "that need no tool):\n"
                + "\n".join(
                    f"- {name}" for name in self._tool_catalog
                )
                + "\n"
            )

        message += (
            "\n"
            f"USER REQUEST:\n{request_text}"
        )

        return await self._orchestrator.generate(
            message=message,
            provider_name=self._provider_name,
            model=self._model,
            conversation_history=conversation_history,
            memory_context=memory_context,
            document_context=document_context,
        )

    def _plan_from_model_output(
        self,
        request_text: str,
        raw: Any,
        planned_at: str,
    ) -> PlanningResult:

        raw_text = raw if isinstance(raw, str) else str(raw)

        payload = self._extract_json(raw_text)

        if payload is None:
            return self._fallback_malformed(
                request_text, raw_text, planned_at
            )

        questions, truncated, dropped = self._clean_questions(
            payload.get("clarifying_questions")
        )

        title = self._clean_text(payload.get("task_title"))
        description = self._clean_text(
            payload.get("task_description")
        )
        steps = self._clean_steps(payload.get("steps"))
        assumptions = self._clean_str_list(
            payload.get("assumptions")
        )

        enough = payload.get("enough_information")
        enough = bool(enough) if isinstance(enough, bool) else None

        has_plan = bool(title)

        if has_plan and enough is not False:
            # A usable plan exists: questions are unnecessary.
            if questions:
                assumptions = assumptions + [
                    "Dropped "
                    f"{len(questions)} model question(s) "
                    "because a complete plan was provided."
                ]
            return PlanningResult(
                request=request_text,
                ready=True,
                task_title=title,
                task_description=description or request_text,
                steps=steps,
                assumptions=assumptions,
                questions=[],
                source=PlanningSource.MODEL,
                parse_ok=True,
                raw_response=raw_text,
                planned_at=planned_at,
            )

        if enough is False or not has_plan:
            if not questions:
                # Never return a dead-end clarification round.
                questions = [
                    "Could you describe the task in a bit "
                    "more detail?"
                ]
            return PlanningResult(
                request=request_text,
                ready=False,
                questions=questions,
                assumptions=assumptions,
                questions_truncated=truncated,
                questions_dropped=dropped,
                source=PlanningSource.MODEL,
                parse_ok=True,
                raw_response=raw_text,
                planned_at=planned_at,
            )

    # ========================================================
    # DETERMINISTIC PATH (no model / fallbacks)
    # ========================================================

    def _deterministic_plan(
        self,
        request_text: str,
        planned_at: str,
    ) -> PlanningResult:

        word_count = len(request_text.split())

        if word_count < _MIN_REQUEST_WORDS:
            return PlanningResult(
                request=request_text,
                ready=False,
                questions=[
                    "Could you describe what you would like "
                    "GHOST to do in a bit more detail?"
                ],
                source=PlanningSource.DETERMINISTIC,
                planned_at=planned_at,
            )

        title = request_text[:_MAX_TITLE_CHARS].strip()

        return PlanningResult(
            request=request_text,
            ready=True,
            task_title=title,
            task_description=request_text,
            steps=[],
            assumptions=[
                "Planner used deterministic defaults "
                "(no model call).",
                "No explicit steps planned; the executor "
                "decides how to carry out the task.",
            ],
            source=PlanningSource.DETERMINISTIC,
            planned_at=planned_at,
        )

    def _fallback_malformed(
        self,
        request_text: str,
        raw_text: str,
        planned_at: str,
    ) -> PlanningResult:

        result = self._deterministic_plan(
            request_text, planned_at
        )

        result.source = PlanningSource.FALLBACK_MALFORMED
        result.parse_ok = False
        result.raw_response = raw_text
        result.assumptions = [
            "Model output was malformed; planner fell back "
            "to deterministic defaults."
        ] + result.assumptions

        return result

    def _fallback_gateway_error(
        self,
        request_text: str,
        planned_at: str,
        exc: Exception,
    ) -> PlanningResult:

        result = self._deterministic_plan(
            request_text, planned_at
        )

        result.source = PlanningSource.FALLBACK_ERROR
        result.parse_ok = False
        result.assumptions = [
            f"Model gateway failed ({type(exc).__name__}); "
            "planner fell back to deterministic defaults."
        ] + result.assumptions

        return result

    # ========================================================
    # NORMALIZATION HELPERS
    # ========================================================

    @staticmethod
    def _extract_json(raw_text: str) -> Optional[dict]:
        """
        Extract the first JSON object from model output,
        tolerating markdown code fences and prose around
        it. Returns None when nothing parseable remains.
        """

        if not raw_text or not raw_text.strip():
            return None

        candidate = raw_text.strip()

        fenced = re.search(
            r"```(?:json)?\s*(\{.*?\})\s*```",
            candidate,
            re.DOTALL,
        )
        if fenced:
            candidate = fenced.group(1)

        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            # Prose around the object: grab the outermost braces.
            match = re.search(
                r"\{.*\}", candidate, re.DOTALL
            )
            if not match:
                return None
            try:
                parsed = json.loads(match.group(0))
            except (ValueError, TypeError):
                return None

        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _clean_questions(raw_questions: Any):
        """
        Filter, dedupe (order-preserving) and enforce the
        absolute 7-question ceiling. Returns
        (questions, truncated, dropped).
        """

        if not isinstance(raw_questions, list):
            return [], False, 0

        seen = set()
        cleaned: List[str] = []

        for item in raw_questions:
            text = str(item).strip() if item is not None else ""
            if not text or text in seen:
                continue
            seen.add(text)
            cleaned.append(text)

        if len(cleaned) <= ABSOLUTE_MAX_QUESTIONS:
            return cleaned, False, 0

        dropped = len(cleaned) - ABSOLUTE_MAX_QUESTIONS

        return (
            cleaned[:ABSOLUTE_MAX_QUESTIONS],
            True,
            dropped,
        )

    @staticmethod
    def _clean_text(value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _clean_str_list(value: Any) -> List[str]:
        if not isinstance(value, list):
            return []
        return [
            str(item).strip()
            for item in value
            if item is not None and str(item).strip()
        ]

    @staticmethod
    def _clean_steps(value: Any) -> List[PlannedStep]:
        """
        Convert model step entries into inert PlannedStep
        objects. Malformed entries are skipped; steps are
        data only and are never executed here.
        """

        if not isinstance(value, list):
            return []

        steps: List[PlannedStep] = []

        for item in value:
            if not isinstance(item, dict):
                continue

            description = str(
                item.get("description", "")
            ).strip()

            if not description:
                continue

            tool = item.get("tool")
            tool = str(tool).strip() if tool else None

            params = item.get("params")
            params = dict(params) if isinstance(params, dict) else {}

            # Dependencies may only reference strictly earlier
            # steps. Anything else (self, forward, malformed)
            # is dropped rather than trusted: the engine will
            # still fail closed if a surviving reference ever
            # cannot be satisfied.
            index = len(steps)
            depends_on = {
                dep
                for dep in item.get("depends_on", [])
                if isinstance(dep, int)
                and not isinstance(dep, bool)
                and 0 <= dep < index
            }

            steps.append(
                PlannedStep(
                    description=description,
                    tool=tool or None,
                    params=params,
                    depends_on=sorted(depends_on),
                )
            )

        return steps
