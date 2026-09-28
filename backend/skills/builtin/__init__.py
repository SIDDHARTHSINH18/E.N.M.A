"""
GHOST builtin skills (M3-F) — first foundational set.

register_builtin_skills() adds their metadata to a
SkillRegistry. This imports the skill modules (they are
small and dependency-free), but catalog/routing itself
still only needs the metadata.
"""

from backend.skills.builtin.document_creation import DocumentCreationSkill
from backend.skills.builtin.memory_recall import MemoryRecallSkill
from backend.skills.builtin.note_summarizer import NoteSummarizerSkill
from backend.skills.builtin.permission_explain import PermissionExplainSkill
from backend.skills.builtin.research import ResearchSkill
from backend.skills.builtin.skill_catalog import SkillCatalogSkill
from backend.skills.builtin.task_breakdown import TaskBreakdownSkill
from backend.skills.registry import SkillRegistry


BUILTIN_SKILLS = (
    MemoryRecallSkill,
    NoteSummarizerSkill,
    DocumentCreationSkill,
    ResearchSkill,
    TaskBreakdownSkill,
    SkillCatalogSkill,
    PermissionExplainSkill,
)


def register_builtin_skills(registry: SkillRegistry) -> None:
    """Register the metadata of every builtin skill."""

    for skill_class in BUILTIN_SKILLS:
        registry.register_metadata(skill_class.metadata)
