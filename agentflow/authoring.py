"""Quality gate for agent-authored reusable prompts (skills, agent instructions).

Self-created skills are injected into every turn of the agents that use them (inside the cached
system prefix), so they must be English, compact and well structured. Hard errors only for
language, size and near-duplicates; structural gaps come back as advisories.
"""
import re

from .language import require_english
from .store import DomainError

SKILL_LIMIT = 6000
DESCRIPTION_LIMIT = 200
INSTRUCTIONS_LIMIT = 3000
SECTIONS = {
    "purpose": r"purpose|when to use|overview|goal",
    "inputs": r"inputs?|prerequisites|requirements",
    "steps": r"steps|procedure|workflow|process|method|instructions",
    "verification": r"verif\w*|checks?|quality|acceptance|validation|tests?",
    "outputs": r"outputs?|deliverables?|results?",
}
FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)


def tidy(text):
    text = str(text or "").replace("\r\n", "\n").replace("\t", "  ")
    text = re.sub(r"[  ]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def words(text):
    return {w for w in re.findall(r"[a-z0-9]+", str(text).casefold()) if len(w) > 2}


def similarity(a, b):
    a, b = words(a), words(b)
    return len(a & b) / len(a | b) if a and b else 0.0


def front_matter(name, description, body):
    """Claude/Codex SKILL.md shape: name + one-line 'when to use' description, then the body."""
    match = FRONT.match(body)
    rest = body[match.end():] if match else body
    meta = {}
    if match:
        for line in match.group(1).splitlines():
            key, _, value = line.partition(":")
            if value.strip():
                meta[key.strip().lower()] = value.strip().strip("\"'")
    name = (name or meta.get("name") or "").strip()
    description = " ".join(str(description or meta.get("description") or "").split())
    slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")[:64] or "custom-skill"
    head = "---\nname: " + slug + "\ndescription: " + description.replace("\n", " ") + "\n---\n\n"
    return head + rest.lstrip(), description


def optimize_skill(doc, existing=(), skill_id=None):
    """Return (doc, advisories) for an agent-authored skill or raise DomainError with a fix."""
    name, description = str(doc.get("name", "")).strip(), " ".join(str(doc.get("description", "")).split())
    body = tidy(doc.get("content", ""))
    require_english("Skill name, description and content", name, description, body)
    if not description:
        raise DomainError("Add a one-line English description that says when to use this skill.")
    if len(description) > DESCRIPTION_LIMIT:
        raise DomainError(f"Shorten the skill description to <= {DESCRIPTION_LIMIT} characters: one line saying when to use it.")
    content, description = front_matter(name, description, body)
    if len(content) > SKILL_LIMIT:
        raise DomainError(f"Condense the skill to <= {SKILL_LIMIT:,} characters (now {len(content):,}). Keep purpose, inputs, "
                          "steps, verification and outputs; remove filler and restated runtime rules; move long examples or "
                          "reference data into a project file and link it.")
    for other in existing:
        if other.get("id") == skill_id or other.get("archived") or not other.get("installed"):
            continue
        score = max(similarity(name, other.get("name", "")),
                    similarity(name + " " + description, other.get("name", "") + " " + other.get("description", "")))
        if score >= 0.6:
            raise DomainError(f"A similar skill exists: {other['id']} ({other.get('name')}). Improve it with skills.update "
                              "and skill_id instead of creating a near-duplicate, or make the name/description clearly distinct.")
    headings = " ".join(re.findall(r"(?m)^\s*(?:#{1,6}\s+|\*\*)([^\n*#]+)", content)).casefold()
    missing = [key for key, pattern in SECTIONS.items() if not re.search(r"\b(?:" + pattern + r")\b", headings)]
    advisories = (["Consider adding sections: " + ", ".join(missing) + "."] if missing else [])
    if description == "Reusable operating guidance authored by the team.":
        advisories.append("Replace the generic description with one line saying when to use this skill.")
    return {**doc, "name": name, "description": description, "content": content}, advisories


def check_agent_text(args):
    """English + size gate for agents.create / agents.update prose fields."""
    require_english("Agent name, role, description, category and instructions",
                    *(args.get(k) for k in ("name", "role", "description", "category", "instructions")))
    if len(str(args.get("instructions") or "")) > INSTRUCTIONS_LIMIT:
        raise DomainError(f"Condense agent instructions to <= {INSTRUCTIONS_LIMIT:,} characters: role, responsibilities, "
                          "working method and quality bar. Put reusable procedures into a skill instead.")
