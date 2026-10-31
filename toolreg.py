"""Tool registry: the ONLY actions the model may ask Jarvis to perform.

The model never runs code. It can only name a registered tool and give arguments; Jarvis then
  1. checks the tool name against the allowlist,
  2. validates every argument against a strict schema (unknown keys, wrong types, out-of-range values are all rejected),
  3. asks you first if the tool is disruptive,
  4. runs the tool's own Python function with a timeout, and
  5. reports exactly what that function returned, success or failure.
"""
import json
import re
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional


class ToolError(Exception):
    """A tool couldn't do what was asked. The message is shown to the user as-is."""


@dataclass
class Param:
    type: str  # "string" | "integer" | "number" | "boolean"
    description: str = ""
    required: bool = True
    enum: Optional[tuple] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    max_length: int = 300
    pattern: Optional[str] = None


@dataclass
class Tool:
    name: str
    description: str
    handler: Callable[[dict], str]
    params: dict = field(default_factory=dict)
    sensitive: bool = False
    confirm_text: Optional[Callable[[dict], str]] = None


@dataclass
class ToolResult:
    ok: bool
    message: str


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class ToolRegistry:
    def __init__(self, log=None):
        self.tools, self.log = {}, log

    def register(self, tool):
        if tool.name in self.tools or tool.name == "none":
            raise ValueError(f"duplicate or reserved tool name: {tool.name}")
        self.tools[tool.name] = tool

    def names(self):
        return sorted(self.tools)

    def validate(self, name, args):
        tool = self.tools.get(name) if isinstance(name, str) else None
        if tool is None:
            raise ToolError(f"'{name}' is not a tool I can use")
        if not isinstance(args, dict):
            raise ToolError(f"{name}: arguments must be an object")
        extra = set(args) - set(tool.params)
        if extra:
            raise ToolError(f"{name}: unexpected argument(s): {', '.join(sorted(map(str, extra)))}")
        clean = {}
        for key, p in tool.params.items():
            if key not in args or args[key] is None:
                if p.required:
                    raise ToolError(f"{name}: missing '{key}'")
                continue
            clean[key] = self._check(name, key, p, args[key])
        return clean

    @staticmethod
    def _check(name, key, p, v):
        where = f"{name}.{key}"
        if p.type == "boolean":
            if not isinstance(v, bool):
                raise ToolError(f"{where} must be true or false")
        elif p.type in ("integer", "number"):
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ToolError(f"{where} must be a number")
            if p.type == "integer":
                if isinstance(v, float):
                    if not v.is_integer():
                        raise ToolError(f"{where} must be a whole number")
                    v = int(v)
            if v != v or v in (float("inf"), float("-inf")):
                raise ToolError(f"{where} must be a finite number")
            if p.minimum is not None and v < p.minimum:
                raise ToolError(f"{where} must be at least {p.minimum}")
            if p.maximum is not None and v > p.maximum:
                raise ToolError(f"{where} must be at most {p.maximum}")
        elif p.type == "string":
            if not isinstance(v, str):
                raise ToolError(f"{where} must be text")
            v = v.strip()
            if _CONTROL.search(v):
                raise ToolError(f"{where} contains control characters")
            if len(v) > p.max_length:
                raise ToolError(f"{where} is too long (max {p.max_length} characters)")
            if p.pattern and not re.fullmatch(p.pattern, v):
                raise ToolError(f"{where} isn't in the expected format")
        else:
            raise ToolError(f"{where}: unsupported type {p.type}")
        if p.enum is not None and v not in p.enum:
            raise ToolError(f"{where} must be one of: {', '.join(map(str, p.enum))}")
        return v

    def schema(self):
        """JSON schema handed to Ollama so the model can only answer in this shape."""
        return {"type": "object", "properties": {"tool": {"type": "string", "enum": self.names() + ["none"]},
                                                 "arguments": {"type": "object"}},
                "required": ["tool", "arguments"]}

    def prompt(self):
        lines = []
        for t in sorted(self.tools.values(), key=lambda t: t.name):
            args = ", ".join(f"{k}: {p.type}{'' if p.required else ' (optional)'}"
                             + (f" one of {'/'.join(map(str, p.enum))}" if p.enum else "") + (f" - {p.description}" if p.description else "")
                             for k, p in t.params.items())
            lines.append(f"- {t.name}({args}): {t.description}")
        return "\n".join(lines)

    def run(self, name, args, confirm, timeout=60):
        """Validate, confirm if needed, execute with a timeout. The message only claims success if the tool itself returned."""
        try:
            args = self.validate(name, args)
        except ToolError as e:
            return ToolResult(False, str(e))
        tool = self.tools[name]
        if tool.sensitive:
            question = tool.confirm_text(args) if tool.confirm_text else f"Run '{name}'?"
            if not confirm(question):
                return ToolResult(False, "Okay, I didn't do it.")
        box = {}

        def work():
            try:
                box["msg"] = tool.handler(args)
            except ToolError as e:
                box["err"] = str(e)
            except Exception as e:
                if self.log:
                    self.log.exception("tool %s crashed", name)
                box["err"] = f"{name} failed unexpectedly ({type(e).__name__}: {e})"

        th = threading.Thread(target=work, name=f"tool-{name}", daemon=True)
        th.start()
        th.join(timeout)
        if th.is_alive():
            return ToolResult(False, f"{name} didn't finish within {timeout:g}s, so I can't tell you whether it worked.")
        if "err" in box:
            return ToolResult(False, box["err"])
        return ToolResult(True, box["msg"])


ACTION_HINT = re.compile(
    r"\b(remind|reminder|timer|alarm|volume|mute|unmute|screenshot|screen ?shot|lock|open|launch|play|calculate|compute|"
    r"memory|remember|recall|what do i|do i have|document|pdf|docx|listen|speak|say|louder|quieter|brightness)\b", re.I)


def looks_actionable(text):
    """Cheap pre-check so ordinary chat doesn't pay for an extra model call."""
    return bool(ACTION_HINT.search(text))


def choose(registry, text, ask_json):
    """Ask the model which tool (if any) fits. Returns (name, args) or None. Never raises."""
    system = ("You decide whether the user's message is a request for one of the tools below.\n"
              'Reply with JSON only: {"tool": "<tool name or none>", "arguments": {...}}.\n'
              'Pick a tool ONLY if the user is clearly asking for that action right now. Chat, questions, opinions and code requests are "none".\n'
              "Tools:\n" + registry.prompt())
    try:
        reply = ask_json([{"role": "system", "content": system}, {"role": "user", "content": text}], registry.schema())
    except Exception:
        return None
    if not isinstance(reply, dict) or reply.get("tool") in (None, "none", ""):
        return None
    return reply.get("tool"), reply.get("arguments") or {}


def run_model_tools(registry, text, ask_json, confirm, say, max_calls=1, chain=False, timeout=60, log=None):
    """Let the model pick up to `max_calls` tools. Returns True if at least one tool ran (so the normal reply is skipped).
    Invalid choices fall back to normal chat (return False) instead of guessing."""
    seen, ran = set(), 0
    prompt_text = text
    for _ in range(max_calls):
        picked = choose(registry, prompt_text, ask_json)
        if not picked:
            break
        name, args = picked
        try:
            args = registry.validate(name, args)
        except ToolError as e:
            if log:
                log.warning("model chose an invalid tool call (%s): %s", name, e)
            break
        key = (name, json.dumps(args, sort_keys=True))
        if key in seen:
            if log:
                log.warning("model repeated tool call %s; stopping", name)
            break
        seen.add(key)
        result = registry.run(name, args, confirm, timeout)
        if log:
            log.info("tool %s %s -> %s", name, args, "ok" if result.ok else result.message)
        say(result.message if result.ok else f"I tried to use {name} but it didn't work: {result.message}")
        ran += 1
        if not chain:
            break
        prompt_text = f"{text}\n(Already done: {result.message}. Reply tool none if the request is fully handled.)"
    return ran > 0
