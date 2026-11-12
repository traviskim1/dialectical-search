"""A stand-in model so the flow and the tests run without one. It knows no activity: every name it
returns is generic, and it reads ids back out of the prompt. For the assess step it reads the person's
words for a few signals, so tests can drive each outcome:
  "scared" / "afraid"            -> afraid
  "instead"                      -> misinterpreted
  "didn't come up"               -> no_chance
  "didn't try"                   -> other
  anything else                  -> engaged
"""

import json
import re


def _ids(prompt: str, prefix: str = "a") -> list[str]:
    seen = []
    for m in re.findall(rf"\[({prefix}\d+)\]", prompt):
        if m not in seen:
            seen.append(m)
    return seen


def _tree(prompt: str) -> str:
    values, actions, n = [], [], 0
    for v in range(1, 4):
        principles = []
        for p in range(1, 3):
            ids = []
            for k in range(1, 4):
                n += 1
                ids.append(f"a{n}")
                actions.append({"id": f"a{n}", "name": f"Action {v}.{p}.{k}", "called": f"move {n}" if k == 1 else "",
                                "what": f"A concrete thing to do ({v}.{p}.{k}).", "why": "It helps.",
                                "overall": min(10, k + v - 1), "principle": min(10, k + p - 1),
                                "impact": 3 + 2 * ((n + k) % 3)})
            if v == 2 and p == 1:
                ids.append("a1")   # one shared action, under two values
            principles.append({"name": f"Principle {v}.{p}", "what": "A part of the value.", "actions": ids})
        values.append({"name": f"Value {v}", "what": "A broad value.", "principles": principles})
    return json.dumps({"values": values, "actions": actions})


def _update(prompt: str) -> str:
    new = prompt.split("NEW, IN THEIR WORDS:", 1)[-1].split("\n\nUpdate what we know.")[0]
    done = []
    if "successfully" in new:
        done = _ids(new)[:1]
    out = {"done": done or (["a1"] if "already" in new else []), "partial": ["a2"] if "sometimes" in new else [],
           "new_actions": [], "knowledge": [], "level": None, "principle_levels": {}, "misc": []}
    if "noticed something new" in new:
        out["new_actions"] = [{"name": "A thing they noticed", "what": "Something new.", "why": "", "principles": ["p1"],
                               "state": "done", "overall": 2, "principle": 2}]
    if "heavier" in new:
        out["misc"] = ["heavier build"]
    return json.dumps(out)


def _assess(prompt: str) -> str:
    out = []
    blocks = re.split(r"\n(?=\[a\d+\])", prompt.split("\n\nFor each [id], decide")[0])
    for b in blocks:
        m = re.match(r"\[(a\d+)\]", b.strip())
        if not m:
            continue
        text = b.lower()
        outcome = ("afraid" if ("scared" in text or "afraid" in text) else "misinterpreted" if "instead" in text
                   else "no_chance" if "didn't come up" in text else "other" if "didn't try" in text else "engaged")
        out.append({"id": m.group(1), "outcome": outcome, "note": "mock",
                    "reply": "" if outcome == "engaged" else "Try it again; here's why it suits you."})
    return json.dumps({"assessed": out})


REPLIES = {
    "intake": lambda p: json.dumps({
        "activity": "the activity", "knowledge": ["does the obvious thing"], "not_enough": ["the other parts"],
        "level": 2, "level_why": "just started", "misc": [],
        "questions": [] if "already answered" in p else ["How long have you been doing it, and how often?"]}),
    "tree": _tree,
    "update": _update,
    "miss": lambda p: json.dumps({"misses": ["the part away from the obvious thing", "the second part"]}),
    "locate misses": lambda p: json.dumps({"located": [{"item": 1, "principle": "p3"}, {"item": 2, "principle": "p5"}]}),
    "choose": lambda p: json.dumps({"chosen": [{"id": i, "why": "mock", "moment": f"the moment for {i}"} for i in _ids(p)]}),
    "expand": lambda p: json.dumps({"cards": [{"id": i, "title": f"Do {i}", "called": "", "scene": ["Imagine you're there."],
                                              "normally": "Normally, you might wait.", "steps": ["1. Do it."], "key": "Do it.",
                                              "why": ["Because."], "fits": "Part of something bigger.",
                                              "enough": "You don't need to get it right for this to count.",
                                              "experiment": ["Try it when it comes up again."], "watch": ""}
                                             for i in _ids(p)]}),
    "guided": lambda p: json.dumps({"questions": [{"id": i, "questions": ["Where was everything when you tried it?"]}
                                                  for i in _ids(p)]}),
    "assess": _assess,
    "backtrack": lambda p: json.dumps({"principle": {"id": None, "name": "Getting comfortable", "what": "", "value": "v1"},
                                       "why": "mock", "actions": [
                                           {"name": "Easier step one", "what": "", "why": "", "overall": 1, "principle": 1},
                                           {"name": "Easier step two", "what": "", "why": "", "overall": 1, "principle": 1}]}),
}


class MockBackend:
    def __init__(self, *_, **__):
        self.model = "mock"
        self.calls: dict[str, int] = {}

    def complete(self, prompt: str, step: str = "", max_tokens: int = 0) -> str:
        key = step.split(" (")[0]
        self.calls[key] = self.calls.get(key, 0) + 1
        return REPLIES[key](prompt)
