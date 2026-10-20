"""Typed registry of the owner settings that can be read and changed from Telegram.

Every entry names a settings key, its type (text, number, bool, selection or model) and its limits. Values are parsed
strictly here and then applied through the existing validators (Service.settings, ModelRouting.set_default,
ApprovalModes.set), so Telegram can never bypass a rule the web app enforces. Credentials are not in the registry and
are refused by name: they are never shown or set from a chat.
"""
import difflib
import math
import re

from .heartbeat import RANGES as HEARTBEAT_RANGES
from .images import ASPECTS, ENGINE_IDS as IMAGE_ENGINES, FORMATS, MAX_COUNT, QUALITIES, SIZES, STYLES
from .safety import BUDGET_RANGES
from .store import DomainError

GROUPS = ("Team", "Models", "Budget", "Heartbeat", "Approvals", "Telegram", "Housekeeping", "Images")
SECRET_WORDS = ("token", "secret", "password", "passwd", "apikey", "api_key", "api-key", "credential", "private_key", "bearer")
TYPES = ("text", "number", "bool", "selection", "model")
TRUE_WORDS = {"on", "true", "yes", "y", "1", "enable", "enabled"}
FALSE_WORDS = {"off", "false", "no", "n", "0", "disable", "disabled"}


class ConfigError(Exception):
    """A rejected value. `options` (list of (value, label)) lets the caller show what is allowed."""

    def __init__(self, message, options=None):
        super().__init__(message)
        self.options = options or []


class Field:
    def __init__(self, key, group, kind, description, path=None, minimum=None, maximum=None, integer=False,
                 max_len=200, allow_empty=False, pattern=None, options=None, special=None, hint=""):
        self.key, self.group, self.kind, self.description = key, group, kind, description
        self.path = tuple(path or key.split("."))
        self.minimum, self.maximum, self.integer = minimum, maximum, integer
        self.max_len, self.allow_empty, self.pattern = max_len, allow_empty, pattern
        self.options, self.special, self.hint = options, special, hint

    def type_label(self):
        if self.kind == "number":
            low = "" if self.minimum is None else self.minimum
            high = "" if self.maximum is None else self.maximum
            return ("whole number " if self.integer else "number ") + "%s-%s" % (low, high)
        if self.kind == "text":
            return "text, max %d chars" % self.max_len
        if self.kind == "bool":
            return "on/off"
        if self.kind == "model":
            return "model"
        return "selection"


def _agents(engine):
    return [(a["id"], a["name"] + " (" + a["id"] + ")") for a in engine.store.all("agents") if not a.get("archived")]


def _models(engine):
    return [(m["provider"] + ":" + m["id"], m.get("name") or m["id"]) for m in engine.models.catalog()]


def _opts(*values):
    return [(v, v) for v in values]


def build():
    low = lambda name: BUDGET_RANGES[name]  # noqa: E731
    fields = [
        Field("workspace_name", "Team", "text", "Workspace name", max_len=120),
        Field("owner_name", "Team", "text", "Owner display name", max_len=120),
        Field("execution_enabled", "Team", "bool", "Let agents run (off pauses all model work)"),
        Field("max_parallel", "Team", "number", "Agents that may work at the same time", minimum=1, maximum=8, integer=True),
        Field("turns_per_slice", "Team", "number", "Model turns per work slice", minimum=1, maximum=24, integer=True),
        Field("adaptive_effort", "Team", "bool", "Adapt reasoning effort to the task"),
        Field("session_compact_tokens", "Team", "number", "Compact a session after this many tokens", minimum=16000, maximum=200000, integer=True),
        Field("quota_policy", "Team", "selection", "When a provider quota runs out", options=_opts("ask", "auto_fallback", "wait")),
        Field("default_model", "Models", "model", "Team default model (new agents and Atlas follow it)", options=_models, special="default_model"),
        Field("model_autonomy", "Models", "selection", "How much agents may change models", options=_opts("locked", "phases", "full")),
    ]
    for name, label in (("run_turns", "Model turns per run"), ("project_turns", "Model turns per project"),
                        ("project_runs", "Runs per project"), ("project_tokens", "Tokens per project (0 = no limit)")):
        fields.append(Field("budget." + name, "Budget", "number", label, minimum=low(name)[0], maximum=low(name)[1], integer=True))
    fields += [
        Field("heartbeat.interval_seconds", "Heartbeat", "number", "Seconds between coordination cycles",
              minimum=HEARTBEAT_RANGES["interval_seconds"][0], maximum=HEARTBEAT_RANGES["interval_seconds"][1], integer=True),
        Field("heartbeat.active_hours.enabled", "Heartbeat", "bool", "Only run scheduled work inside active hours"),
        Field("heartbeat.active_hours.start", "Heartbeat", "text", "Active hours start (HH:MM, GMT+7)", max_len=5, pattern=r"([01]\d|2[0-3]):[0-5]\d"),
        Field("heartbeat.active_hours.end", "Heartbeat", "text", "Active hours end (HH:MM, GMT+7)", max_len=5, pattern=r"([01]\d|2[0-3]):[0-5]\d"),
        Field("heartbeat.pause_schedules_outside_hours", "Heartbeat", "bool", "Pause scheduled work outside active hours"),
        Field("heartbeat.pulse.enabled", "Heartbeat", "bool", "Team pulse on/off"),
        Field("heartbeat.pulse.every_minutes", "Heartbeat", "number", "Minutes between team pulses",
              minimum=HEARTBEAT_RANGES["every_minutes"][0], maximum=HEARTBEAT_RANGES["every_minutes"][1], integer=True),
        Field("heartbeat.pulse.only_if_changed", "Heartbeat", "bool", "Pulse only when something changed"),
        Field("heartbeat.pulse.notify", "Heartbeat", "selection", "When the pulse may message you", options=_opts("needed", "always")),
        Field("heartbeat.pulse.agent_id", "Heartbeat", "selection", "Agent that runs the pulse", options=_agents),
        Field("approvals.mode", "Approvals", "selection", "Approval mode for this chat (ask or always approve)", options=_opts("ask", "auto"), special="approval_mode"),
        Field("telegram_mode", "Telegram", "selection", "Telegram connection (restart to apply)", options=_opts("polling", "webhook")),
        Field("telegram.default_agent_id", "Telegram", "selection", "Agent that receives plain messages", options=_agents),
        Field("telegram.forward.questions", "Telegram", "bool", "Send agent questions to Telegram"),
        Field("telegram.forward.permissions", "Telegram", "bool", "Send permission requests to Telegram"),
        Field("telegram.forward.agent_messages", "Telegram", "bool", "Send agent messages to Telegram"),
        Field("telegram.forward.status_digest", "Telegram", "bool", "Send status digests to Telegram"),
        Field("telegram.forward.files", "Telegram", "bool", "Send deliverable files to Telegram"),
        Field("housekeeping.auto_cleanup", "Housekeeping", "bool", "Clean temporary files automatically"),
        Field("housekeeping.min_free_gb", "Housekeeping", "number", "Ask to clean when free disk is below (GB)", minimum=1, maximum=500),
        Field("housekeeping.retention_days", "Housekeeping", "number", "Days to keep temporary files", minimum=1, maximum=365, integer=True),
        Field("image.mode", "Images", "selection", "Image engine choice", options=_opts("agent", "fixed")),
        Field("image.engine", "Images", "selection", "Fixed image engine", options=_opts(*IMAGE_ENGINES)),
        Field("image.model", "Images", "text", "Fixed image model (empty clears it)", max_len=120, allow_empty=True, pattern=r"[A-Za-z0-9._:/@+-]{1,120}"),
        Field("image.defaults.aspect_ratio", "Images", "selection", "Default aspect ratio", options=_opts(*(ASPECTS + ("auto",)))),
        Field("image.defaults.size", "Images", "selection", "Default size", options=_opts(*SIZES)),
        Field("image.defaults.quality", "Images", "selection", "Default quality", options=_opts(*QUALITIES)),
        Field("image.defaults.format", "Images", "selection", "Default format", options=_opts(*FORMATS)),
        Field("image.defaults.count", "Images", "number", "Default image count", minimum=1, maximum=MAX_COUNT, integer=True),
        Field("image.defaults.style", "Images", "selection", "Default style preset", options=_opts(*STYLES)),
    ]
    return fields


FIELDS = build()
BY_KEY = {f.key.casefold(): f for f in FIELDS}


def is_secret_key(key):
    folded = str(key).casefold().replace(" ", "_")
    return any(word in folded for word in SECRET_WORDS)


def find(key):
    """The field for `key` (case-insensitive). Raises ConfigError for secrets and unknown keys, with close matches."""
    text = str(key or "").strip()
    if not text:
        raise ConfigError("Give a setting key. /showall lists them.")
    field = BY_KEY.get(text.casefold())
    if field:   # only registered settings are ever readable or writable, so e.g. budget.project_tokens is fine
        return field
    if is_secret_key(text):
        raise ConfigError("Secrets and credentials are never shown or changed from Telegram.")
    near = difflib.get_close_matches(text.casefold(), list(BY_KEY), n=4, cutoff=0.5)
    raise ConfigError("Unknown setting: %s.%s" % (text[:60], (" Did you mean " + ", ".join(BY_KEY[n].key for n in near) + "?") if near else " /showall lists every setting."))


def options_of(engine, field):
    value = field.options
    return list(value(engine) if callable(value) else value or [])


# ---- reading ----------------------------------------------------------------------------------------------

def _digging(data, path):
    for part in path:
        if not isinstance(data, dict) or part not in data:
            return None
        data = data[part]
    return data


def current(engine, field):
    settings = engine.store.settings()
    root = field.path[0]
    if field.special == "default_model":
        binding = engine.default_binding()
        return (binding["provider"] + ":" + binding["model"]) if binding.get("model") else ""
    if field.special == "approval_mode":
        return engine.approvals.modes().get(approval_channel(engine), "ask")
    if root == "budget":
        return engine.safety.budget().get(field.path[1])
    if root == "heartbeat":
        return _digging(engine.heartbeat.config(), field.path[1:])
    if root == "housekeeping":
        return _digging(engine.housekeeping.config(), field.path[1:])
    if root == "image":
        return _digging(engine.images.current(), field.path[1:])
    if root == "telegram":
        return _digging(engine.telegram.config(), field.path[1:])
    if field.key == "telegram_mode":
        return engine.telegram.mode()
    defaults = {"execution_enabled": True, "max_parallel": 4, "turns_per_slice": 6, "adaptive_effort": False,
                "session_compact_tokens": 80000, "quota_policy": "ask", "model_autonomy": "locked"}
    return settings.get(field.key, defaults.get(field.key, ""))


def approval_channel(engine):
    return "dm-" + engine.telegram.default_agent()


def show(value, field=None):
    if isinstance(value, bool):
        return "on" if value else "off"
    if value is None or value == "":
        return "(empty)"
    return str(value)


# ---- parsing ----------------------------------------------------------------------------------------------

def parse_number(text, integer=False, minimum=None, maximum=None):
    """"1.5", "1,5", "2 000", "1,000" (whole numbers) and "5.0" are accepted; "5.5" for a whole number is not."""
    bad = ConfigError("Enter a number, for example 5 or 1.5.")
    raw = str(text).strip().replace(" ", "").replace("\u00a0", "").replace("_", "")
    if not re.fullmatch(r"[+-]?[0-9]+(?:[.,][0-9]+)*", raw):
        raise bad
    sign = -1 if raw.startswith("-") else 1
    body = raw.lstrip("+-")
    separators = re.findall(r"[.,]", body)
    groups = re.split(r"[.,]", body)
    grouped = all(len(g) == 3 for g in groups[1:]) and 1 <= len(groups[0]) <= 3
    if not separators:
        value = float(body)
    elif len(set(separators)) == 1 and len(separators) == 1:
        # one separator: thousands grouping for whole-number settings ("1,000"), otherwise a decimal point or comma
        value = float("".join(groups)) if integer and grouped else float(groups[0] + "." + groups[1])
    elif len(set(separators)) == 1:
        if not grouped:
            raise bad
        value = float("".join(groups))                       # 1,000,000
    else:
        thousands, decimal = separators[0], separators[-1]
        if separators.count(decimal) != 1 or thousands == decimal or not grouped_with(body, decimal):
            raise bad
        value = float(body.replace(thousands, "").replace(decimal, "."))   # 1.234,5 or 1,234.5
    value *= sign
    if not math.isfinite(value):
        raise ConfigError("Enter a finite number.")
    if integer and value != int(value):
        raise ConfigError("Enter a whole number (no decimals).")
    if value == int(value):
        value = int(value)
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        raise ConfigError("Enter a value from %s to %s." % (minimum if minimum is not None else "-inf", maximum if maximum is not None else "inf"))
    return value


def grouped_with(body, decimal):
    """True when everything before the decimal separator is properly grouped in threes."""
    whole = body.rsplit(decimal, 1)[0]
    parts = re.split(r"[.,]", whole)
    return 1 <= len(parts[0]) <= 3 and all(len(p) == 3 for p in parts[1:])


def match_option(text, options):
    folded = str(text).strip().casefold()
    for value, label in options:
        if folded in (str(value).casefold(), str(label).casefold()):
            return value
    for value, label in options:  # agents: also accept the bare name before " (id)"
        if folded == str(label).casefold().split(" (")[0]:
            return value
    return None


def parse(engine, field, text):
    """Typed, validated value for `field` from the owner's text; raises ConfigError otherwise."""
    value = str(text if text is not None else "")
    if field.kind == "number":
        return parse_number(value, field.integer, field.minimum, field.maximum)
    if field.kind == "bool":
        word = value.strip().casefold()
        if word in TRUE_WORDS:
            return True
        if word in FALSE_WORDS:
            return False
        raise ConfigError("Choose on or off.", [("on", "on"), ("off", "off")])
    if field.kind == "text":
        clean = value.strip()
        if any(ord(c) < 32 for c in clean):
            raise ConfigError("The text cannot contain control characters or line breaks.")
        if not clean and not field.allow_empty:
            raise ConfigError("The value cannot be empty.")
        if len(clean) > field.max_len:
            raise ConfigError("Use at most %d characters (you sent %d)." % (field.max_len, len(clean)))
        if clean and field.pattern and not re.fullmatch(field.pattern, clean):
            raise ConfigError("That is not a valid value for this setting.")
        return clean
    options = options_of(engine, field)
    if field.kind == "model":
        found = match_option(value, options)
        if found is None:
            bare = [(v, l) for v, l in options if v.split(":", 1)[-1].casefold() == value.strip().casefold()]
            if len(bare) == 1:
                found = bare[0][0]
            elif len(bare) > 1:
                raise ConfigError("Several providers have that model. Use provider:model.", bare[:12])
        if found is None:
            raise ConfigError("That model is not in the catalog.", options[:12])
        return found
    found = match_option(value, options)
    if found is None:
        raise ConfigError("Choose one of the listed values.", options[:12])
    return found


# ---- applying ---------------------------------------------------------------------------------------------

def nested(path, value):
    result = value
    for part in reversed(path):
        result = {part: result}
    return result


def apply(engine, field, value, service=None):
    """Write the value through the same validators the web app uses. Returns (old, new) as display strings."""
    from .service import Service
    service = service or Service(engine.store, engine.providers, engine.vault, engine)
    old = show(current(engine, field))
    try:
        if field.special == "default_model":
            provider, _, model = value.partition(":")
            binding = engine.models.resolve({"provider": provider, "model": model})
            engine.models.set_default(binding, source="owner_command", actor_id="owner")
        elif field.special == "approval_mode":
            engine.approvals.set(approval_channel(engine), value)
        else:
            service.settings(nested(field.path, value))
    except DomainError as exc:
        raise ConfigError(str(exc)) from None
    new = show(current(engine, field))
    engine.store.event("Setting changed from Telegram", "%s: %s -> %s" % (field.key, old, new), "settings")
    return old, new


def listing(engine):
    """[(group, [(key, display value, type label)])] for /showall; reading a value never raises."""
    groups = {name: [] for name in GROUPS}
    for field in FIELDS:
        try:
            shown = show(current(engine, field))
        except Exception:
            shown = "(unavailable)"
        groups[field.group].append((field.key, shown, field.type_label()))
    return [(name, rows) for name, rows in groups.items() if rows]
