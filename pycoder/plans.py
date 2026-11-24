"""Bounded nested plans shared by tools and presentation."""

STATUSES = ["pending", "in_progress", "completed"]


def step_schema(depth=0):
    properties = {"step": {"type": "string"}, "status": {"type": "string", "enum": STATUSES}}
    if depth < 2:
        properties["children"] = {"type": "array", "items": step_schema(depth + 1)}
    return {"type": "object", "properties": properties, "required": ["step", "status"], "additionalProperties": False}


def validate_plan(plan):
    count = 0
    def check(items, depth=0, parent=None):
        nonlocal count
        if not isinstance(items, list) or len(items) > 30 or depth > 2:
            raise ValueError("Invalid plan: max 30 siblings, 3 levels")
        if sum(isinstance(p, dict) and p.get("status") == "in_progress" for p in items) > 1:
            raise ValueError("Only one plan step per level may be in_progress")
        for p in items:
            count += 1
            if (count > 90 or not isinstance(p, dict) or set(p) - {"step", "status", "children"}
                    or not isinstance(p.get("step"), str) or not p["step"].strip()
                    or p.get("status") not in STATUSES):
                raise ValueError("Invalid plan")
            if parent == "completed" and p["status"] != "completed":
                raise ValueError("Completed steps must have completed children")
            if p["status"] == "in_progress" and parent not in (None, "in_progress"):
                raise ValueError("Active children require an in_progress parent")
            if "children" in p:
                check(p["children"], depth + 1, p["status"])
    check(plan)


def plan_lines(plan, depth=0):
    markers = {"pending": "○", "in_progress": "◉", "completed": "✓"}
    for p in plan:
        yield "  " * depth + f"{markers[p['status']]} {p['step']}"
        yield from plan_lines(p.get("children", []), depth + 1)


def has_open_steps(plan):
    return any(p["status"] != "completed" or has_open_steps(p.get("children", [])) for p in plan)


FINAL_PLAN_CHECK = """Runtime plan check (not a new user task): the plan still has unfinished steps.
Before your final answer, reconcile the plan with actual tool results using update_plan.
Mark a step completed only when its work and required checks are actually done.
Do not repeat completed actions or invent successful checks to close the plan.
If work is blocked, deferred by the user, or belongs to an unrelated earlier task,
leave those steps unfinished and explain the reason and remaining work in your final answer.
Do not resume unrelated work just to clear an old plan.
Current plan (data):
"""
