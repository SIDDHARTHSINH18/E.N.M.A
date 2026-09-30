import logging

from fastapi import APIRouter, Request

# Singletons owned elsewhere and reused here (read-only):
# the one task store backing /api/tasks and the one
# production tool registry. No second instance is created.
from backend.api.tasks import task_service
from backend.core.security import hash_session_token
from backend.core.agent_services import tool_registry
from backend.core.services import documents, memory_service


logger = logging.getLogger(
    "ghost.graph",
)


router = APIRouter(
    prefix="/api",
    tags=["Graph"],
)


# Privacy (docs/05-privacy-assessment.md §3.4): the graph
# previously returned full memory content to any caller.
# It now returns counts and truncated labels only; memory
# content is available exclusively through the
# authenticated memory API (M1).

MAX_MEMORY_NODES = 12

MAX_TASK_NODES = 12

MAX_LABEL_LENGTH = 38


def shorten(
    text,
    length=45,
):

    text = (
        str(text)
        .replace("\n", " ")
        .strip()
    )

    if len(text) <= length:
        return text

    return (
        text[:length - 3]
        + "..."
    )


@router.get("/graph")
async def get_graph(http_request: Request):
    """
    Return the current GHOST knowledge/access graph.

    Every node reflects real application state:
    - GHOST core
    - documents (from the document store)
    - memories (labels only, never content)
    - projects (derived from real memory.project values)
    - tools (the production tool registry)
    - tasks (the live task store)
    """

    nodes = []
    edges = []

    # ============================================================
    # GHOST CORE
    # ============================================================

    nodes.append({
        "id": "ENMA",
        "type": "ENMA",
        "label": "ENMA",
        "description": "Personal AI operating system.",
    })

    # ============================================================
    # DOCUMENTS (metadata only, never document text)
    # ============================================================

    for document_id, document in documents.items():

        filename = document.get(
            "filename",
            "Unknown document",
        )

        chunks = document.get(
            "chunks",
            [],
        )

        pages = document.get(
            "pages",
            [],
        )

        node_id = f"document-{document_id}"

        nodes.append({
            "id": node_id,
            "type": "document",
            "label": filename,
            "description": (
                "Indexed document available to GHOST."
            ),
            "document_id": document_id,
            "size": document.get(
                "size",
                0,
            ),
            "chunks": len(chunks),
            "pages": len(pages),
            "status": "indexed",
        })

        edges.append({
            "source": "GHOST",
            "target": node_id,
            "type": "access",
        })

    # ============================================================
    # MEMORY (counts + truncated labels only)
    # ============================================================

    memory_root = {
        "id": "memory-root",
        "type": "memory",
        "label": "Memory",
        "description": (
            "GHOST's persistent conversation memory."
        ),
    }

    nodes.append(memory_root)

    edges.append({
        "source": "GHOST",
        "target": "memory-root",
        "type": "access",
    })

    memory_items = []

    try:

        # Single shared instance (defect D3 fixed in M0);
        # read-only access here.
        memory_items = list(
            memory_service.memories
        )

    except Exception as error:

        logger.warning(
            "Graph memory read failed: %s",
            type(error).__name__,
        )

    nodes.append({
        "id": "memory-count",
        "type": "memory",
        "label": f"{len(memory_items)} memories",
        "description": (
            f"{len(memory_items)} stored memory items."
        ),
    })

    edges.append({
        "source": "memory-root",
        "target": "memory-count",
        "type": "contains",
    })

    # Display up to MAX_MEMORY_NODES label-only nodes.
    # Labels are derived from type/index only — never
    # from content: short memories would otherwise
    # leak verbatim through the label (caught by
    # tests/test_main.py::test_graph_memory_labels_not_content).
    #
    # Node ids are the REAL memory ids so the frontend can
    # resolve the full memory item through the memory API
    # when a node is selected.
    memory_projects = set()

    for index, memory in enumerate(
        memory_items[:MAX_MEMORY_NODES]
    ):

        if not isinstance(
            memory,
            dict,
        ):
            continue

        memory_type = memory.get(
            "memory_type",
        ) or memory.get(
            "type",
            "conversation",
        )

        memory_id = str(
            memory.get(
                "id",
            )
            or f"memory-item-{index}"
        )

        node_id = f"memory-{memory_id}"

        nodes.append({
            "id": node_id,
            "type": "memory",
            "label": shorten(
                f"memory · {memory_type}",
                MAX_LABEL_LENGTH,
            ),
            "description": (
                "Memory item (content hidden; use the "
                "memory API to inspect it)."
            ),
            "memory_id": memory_id,
            "memory_type": memory_type,
        })

        edges.append({
            "source": "memory-root",
            "target": node_id,
            "type": "contains",
        })

        # Real relationship: memories may declare the
        # project they belong to (memory.project field).
        project = memory.get(
            "project",
        )

        if project:

            memory_projects.add(
                str(project),
            )

            edges.append({
                "source": node_id,
                "target": f"project-{project}",
                "type": "project_of",
            })

    # ============================================================
    # PROJECTS
    #
    # The GHOST system project always exists; additional
    # project nodes appear only when real memories
    # reference them through the project field.
    # ============================================================

    nodes.append({
        "id": "project-ENMA",
        "type": "project",
        "label": "ENMA",
        "description": (
            "ENMA personal AI operating system."
        ),
    })

    edges.append({
        "source": "GHOST",
        "target": "project-ENMA",
        "type": "project",
    })

    for project in sorted(
        memory_projects - {"GHOST"}
    ):

        nodes.append({
            "id": f"project-{project}",
            "type": "project",
            "label": shorten(
                project,
                MAX_LABEL_LENGTH,
            ),
            "description": (
                "Project referenced by stored memories."
            ),
        })

        edges.append({
            "source": "GHOST",
            "target": f"project-{project}",
            "type": "project",
        })

    # ============================================================
    # TOOLS (production tool registry — real capabilities)
    # ============================================================

    for tool in tool_registry.list_tools():

        node_id = f"tool-{tool.name}"

        nodes.append({
            "id": node_id,
            "type": "tool",
            "label": tool.name,
            "description": tool.description,
            "category": tool.category,
            "risk_level": tool.risk_level.value,
        })

        edges.append({
            "source": "GHOST",
            "target": node_id,
            "type": "tool",
        })

    # ============================================================
    # TASKS (live task store — real planned/executed tasks)
    # ============================================================

    # Ownership: only the caller's tasks (plus legacy
    # owner-less tasks) may appear in the graph.
    token = getattr(http_request.state, "session_token", "")
    owner = hash_session_token(token) if token else None
    tasks = task_service.list(owner=owner)

    nodes.append({
        "id": "tasks-root",
        "type": "task",
        "label": "Tasks",
        "description": (
            "GHOST task execution system."
        ),
        "status": "active",
    })

    edges.append({
        "source": "GHOST",
        "target": "tasks-root",
        "type": "tasks",
    })

    for task in tasks[-MAX_TASK_NODES:]:

        node_id = f"task-{task.id}"

        nodes.append({
            "id": node_id,
            "type": "task",
            "label": shorten(
                task.title,
                MAX_LABEL_LENGTH,
            ),
            "description": shorten(
                task.description,
                120,
            ),
            "task_id": task.id,
            "status": task.status.value,
            "priority": task.priority,
            "created_at": task.created_at.isoformat(),
        })

        edges.append({
            "source": "tasks-root",
            "target": node_id,
            "type": "contains",
        })

    # ============================================================
    # STATISTICS
    # ============================================================

    return {
        "success": True,
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "documents": len(documents),
            "memories": len(memory_items),
            "projects": 1 + len(memory_projects - {"GHOST"}),
            "tools": len(tool_registry.list_tools()),
            "tasks": len(tasks),
        },
    }
