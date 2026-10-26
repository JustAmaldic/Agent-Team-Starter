"""Safe deterministic task risk assessment and bounded planning suggestions.

No model is trusted to relax these controls. These are preflight hints, not approval.
"""
import re

CATEGORIES = {
    "python": (r"\bpython\b", r"\.py\b", r"\bpytest\b"),
    "frontend": (r"\bfrontend\b", r"\bcss\b", r"\bui\b", r"\breact\b", r"\bjavascript\b"),
    "testing": (r"\btest(?:ing|s)?\b", r"\bpytest\b", r"\bunittest\b"),
    "architecture": (r"\barchitecture\b", r"\barchitectuur\b", r"\bdesign\b"),
    "refactoring": (r"\brefactor(?:ing)?\b", r"\bmodularis", r"\bopsplits"),
    "documentation": (r"\bdocumentatie\b", r"\bdocs?\b", r"\breadme\b"),
}
RISK = {
    "security": (r"\bauth(?:entication|orization)?\b", r"\boauth\b", r"\btoken\b", r"\bsecret\b", r"\bpermission\b", r"\bbeveilig"),
    "database": (r"\bdatabase\b", r"\bmigrat", r"\bsql\b", r"\bschema\b"),
    "financial": (r"\btrading\b", r"\borders?\b", r"\bwallet\b", r"\bpayment\b", r"\bgeld\b"),
    "deployment": (r"\bdeploy\b", r"\bproductie\b", r"\bproduction\b", r"\bdns\b"),
    "destructive": (r"\bdelete\b", r"\bverwijder\b", r"\bwipe\b", r"\bdrop table\b", r"\bforce.push\b"),
}
REQUIRED_CHECKS = {
    "security": ["Authorization and negative-access regression tests", "Secrets and permission audit"],
    "database": ["Migration upgrade/rollback test", "Backup and data integrity review"],
    "financial": ["Paper-mode integration tests", "Order idempotency and risk-limit checks"],
    "deployment": ["Rollback and staging smoke test"],
    "destructive": ["Dry-run and explicit human approval"],
}


def classify_task(task):
    text = task.lower()
    categories = [name for name, patterns in CATEGORIES.items()
                  if any(re.search(p, text) for p in patterns)] or ["general"]
    risks = [name for name, patterns in RISK.items()
             if any(re.search(p, text) for p in patterns)]
    required = list(dict.fromkeys(check for risk in risks
                                  for check in REQUIRED_CHECKS[risk]))
    level = "high" if risks else ("medium" if len(categories) > 2 or len(task) > 500 else "low")
    return {"categories": categories, "level": level, "risks": risks,
            "required_checks": required,
            "human_gate": bool(risks), "auto_approve_knowledge": not risks}


def suggest_plan(task):
    profile = classify_task(task)
    checks = ["Existing project test_commands", *profile["required_checks"]]
    complexity = "L" if profile["level"] == "high" or len(task) > 600 else "M" if len(task) > 130 else "S"
    steps = [{"id": "scope", "title": "Inspect relevant code and acceptance criteria",
              "depends_on": [], "checks": [], "agent": "auto"}]
    categories = profile["categories"]
    risks = profile["risks"]
    additions = []
    if "security" in risks:
        additions.append(("security", "Map authentication, authorization and trust boundaries"))
    if "database" in risks:
        additions.append(("migration", "Plan reversible migration and data integrity checks"))
    if "financial" in risks:
        additions.append(("financial", "Validate order safety and paper-mode risk limits"))
    if "frontend" in categories:
        additions.append(("interface", "Inspect existing components, style tokens and accessibility"))
    if "architecture" in categories or "refactoring" in categories:
        additions.append(("architecture", "Identify module boundaries and compatibility risks"))
    if "testing" in categories:
        additions.append(("testplan", "Select representative unit and integration tests"))
    for ident, title in additions:
        steps.append({"id": ident, "title": title,
                      "depends_on": ["scope"], "checks": [], "agent": "auto"})
    dependencies = [item[0] for item in additions] or ["scope"]
    steps.extend([
        {"id": "implementation", "title": "Implement the requested change in bounded parts",
         "depends_on": dependencies, "checks": [], "agent": "auto"},
        {"id": "verification", "title": "Run tests and independent review",
         "depends_on": ["implementation"], "checks": checks, "agent": "auto"}])
    return {"task": task, "complexity": complexity, "risk": profile,
            "steps": steps}



def validate_plan(plan):
    steps = plan.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 20:
        raise ValueError("Plan must contain 1 to 20 steps")
    ids = [s.get("id") for s in steps if isinstance(s, dict)]
    if len(ids) != len(steps) or any(not isinstance(i, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,50}", i) for i in ids) or len(set(ids)) != len(ids):
        raise ValueError("Plan step IDs must be unique and safe")
    seen = set()
    for step in steps:
        if not isinstance(step.get("title"), str) or not 3 <= len(step["title"]) <= 300:
            raise ValueError("Invalid step title")
        deps = step.get("depends_on", [])
        if not isinstance(deps, list) or any(d not in seen for d in deps):
            raise ValueError("Dependencies must refer to preceding steps")
        seen.add(step["id"])
    return True
