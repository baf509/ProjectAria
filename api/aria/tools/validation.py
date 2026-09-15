"""Configuration checks without launching tools or optional services."""
from datetime import datetime, timezone

from aria.config import settings


def missing_tool_names(names, registered):
    registered = set(registered)
    return sorted({name for name in (names or []) if not (
        any(t.startswith(name[:-1]) for t in registered) if name.endswith("*")
        else name in registered
    )})


async def tool_configuration_report(db, router):
    registered = {t.name for t in router.list_tools()}
    agents = []
    async for agent in db.agents.find({}, {"slug": 1, "enabled": 1, "capabilities": 1, "enabled_tools": 1}):
        active = bool(agent.get("enabled", True) and agent.get("capabilities", {}).get("tools_enabled"))
        names = agent.get("enabled_tools") or []
        agents.append({"slug": agent.get("slug"), "enabled": active,
                       "configured": names, "unregistered": missing_tool_names(names, registered)})
    # These tools deliberately depend on the optional Claude CLI. Keep that
    # distinction visible without treating an unused integration as a failure.
    optional = {"claude_agent", "deep_think"}
    absent = set(settings.tool_allowed_names) - registered
    problems = [{"agent": a["slug"], "unregistered": a["unregistered"]}
                for a in agents if a["enabled"] and a["unregistered"]]
    stale_policy = sorted(absent - optional)
    return {"ok": not problems and not stale_policy,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "registered": sorted(registered), "agents": agents,
            "problems": problems, "unregistered_allowlist": stale_policy,
            "unavailable_optional": sorted(absent & optional),
            "execution_tested": False}
