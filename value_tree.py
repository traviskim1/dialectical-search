"""
Dialectical Search v6: the value tree.
Author: Claude, for Travis. Date: 11/10/26. Spec: SPEC_v6.md (Travis's 10/28 overhaul).

v6 replaces v5's flat polygon of 8-12 values with a tree, and replaces most of v5's backend with
one small loop. Everything the app does is here; app.py only draws it.

THE TREE
    root: the activity
      dialectical values   broad things no one can be told into understanding (they're the sum of time spent)
        principles         aspects of a value tied to the activity's own situations
          actions          concrete things a newcomer can do next session; often have a name
    An action can sit under several principles (even under different values); it is stored once and
    every parent points at it, so crossing it off crosses it off everywhere.
    Each action has two 1-10 difficulty ratings:
      "overall"    general experience in the activity it needs
      "principle"  how much of the rest of its principle(s) it needs
    and one 1-10 "impact" rating: how much doing it changes how the activity goes (for the person's
    goals and for the people they do it with). Within reach, higher impact is suggested first.

THE USER PROFILE (stored in two places, as the spec says)
    in the tree:      each action's state: open / partial (downweighted) / done (crossed off)
    outside the tree: overall level (1-10), per-principle levels, knowledge lines, misc details

ONE ROUND (the spec's steps 4-8)
    1. score every principle: gap = 1 - (done + 0.5*partial) / number of actions under it
       blend with the "what do people like this usually miss?" query, weighted by
       MISS_WEIGHT / (1 + rounds already closed): heavy at the start, lighter as context builds
    2. shortlist open actions that are within reach (overall <= level + AWARENESS + STRETCH and
       principle <= that principle's level + AWARENESS + STRETCH), from every principle, ranked by the
       principle's score plus the action's impact
    3. the model picks 2-3 from the shortlist using the misc details, and writes each card
    4. after doing the activity: free feedback per card, then guided questions per card,
       then general feedback
    5. close: the model classifies each card (engaged / misinterpreted / afraid / no_chance / other),
       updates the profile and the tree from everything written, and writes a reply for failures.
       A failed action comes back next round with that reply. After MAX_ATTEMPTS failures the app
       backtracks: it names what's in the way as a principle, adds easier actions toward it, and puts
       the original action behind them.

TEMPORARY: PILOT MODE (added 11/12/26 for a one-off test session on 11/13/26; not part of the design)
    About 15 people try the app for ten minutes each, pretending to be new to an activity. They don't
    go and do anything, so instead of the feedback steps above (free, guided, general, then close_round)
    the page asks them to judge each suggestion (the PILOT_* questions) and review_round() moves on as
    if they had done every card. Their answers are for Travis, to improve the app; the model never
    sees them. MODE = "study" (or DTR_MODE=study) puts the real flow back; nothing in it was changed.
    Everything pilot-only is marked PILOT, here and in app.py, and can be deleted once the test is over.

SPEED (none of this changes a prompt or what the model is asked)
    The tree is the slowest call (roughly 25-70 s of a 90 s first round) and needs only the activity's
    name, which the first intake gives. So when there are follow-up questions, the tree is built in a
    background thread while the person answers them (PREFETCH_TREE). The database connection is kept
    open between calls.
    Three more, which did change what the model is given (Travis's choice, 11/12/26):
      - the "usually miss" query (and then placing its answers on the tree) runs in the background
        alongside the profile update, so it reads the profile as it was before the update;
      - each card is written in its own call, all at the same time. So that the cards don't collide,
        the choose call gives each action its own moment, a retried action keeps the scene it had, and
        every card call is given all the moments.
"""

from __future__ import annotations

import datetime
import json
import os
import random
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

try:
    from dotenv import load_dotenv  # optional: settings from .env
    load_dotenv()
except ImportError:
    pass

# =============================================================================
# SETTINGS (small numbers the spec leaves open; change here)
# =============================================================================
SEARCH_PROMPT = ("Tell me about an activity you are interested in. Describe the ways you have engaged with it "
                 "in the past, things you feel you are not doing enough and things you feel you already "
                 "understand well.")
MAX_QUESTIONS = 3            # follow-up questions after the first answer, at most, asked once
QUESTION_WORDS = 20          # longer questions are dropped
VALUES_RANGE = (3, 5)        # dialectical values under the root
PRINCIPLES_RANGE = (2, 4)    # principles under each value
ACTIONS_RANGE = (2, 4)       # actions under each principle (selective: the high-impact, less obvious ones)
CARDS = 3                    # actions per round (2-3; retries count toward this)
MIN_CARDS = 2
STRETCH = 2                  # how far above the person's level an action may be
IMPACT_WEIGHT = 0.5          # shortlist score: impact 10 adds this (impact 0 takes it away) on top of the principle's need
STRETCH_WEIGHT = 0.05        # shortlist score: taken off per point of stretch cost
PARTIAL_PENALTY = 0.25       # shortlist score: taken off a partial (half-known) action
AWARENESS = 1                # assume the person knows this much more than their stated level (suggest past the basics)
MISS_WEIGHT = 0.6            # weight of the "what do people like this miss" query before any round is closed
MISSES_ASKED = 6
PARTIAL_CREDIT = 0.5         # a partial (downweighted) action counts this much toward a principle's coverage
SHORTLIST = 8
MAX_ATTEMPTS = 3             # failed tries of one action before backtracking
PREFETCH_TREE = True         # build the tree while the person answers the follow-up questions (same call, started earlier)
STATES = ("open", "partial", "done")
OUTCOMES = ("engaged", "misinterpreted", "afraid", "no_chance", "other")
FAILURES = ("misinterpreted", "afraid", "other")     # count toward MAX_ATTEMPTS; no_chance doesn't

# ---- TEMPORARY: PILOT MODE (a one-off test session on 11/13/26; see the note at the top of this file) ----
# "pilot": testers judge the suggestions instead of trying them. "study": the real flow (the spec's).
# A journal remembers the mode it was started in (j["mode"]), so switching back never breaks an open one.
MODE = os.getenv("DTR_MODE", "pilot")
PILOT_INTRO = ("This is a test run of an early version. It takes about 10 minutes. Pick an activity that interests "
               "you and answer as if you were fairly new to it. You won't be asked to go and do anything: you'll "
               "get a few suggestions and be asked what you think of them. What you write is used to improve the app.")
PILOT_RATING = "How much would this suggestion help someone new appreciate {activity} at a deeper level?"
PILOT_SCALE = "1 = not at all, 5 = a great deal"
PILOT_HELPS = ("How does this suggestion help, or fail to help, someone appreciate {activity} more or see it in "
               "a new way?")
PILOT_INSTEAD = "What would you have liked to see suggested instead?"
PILOT_DEEPER = ("What is something that would be very helpful for getting you to appreciate {activity} at a deeper "
                "level, or a suggestion that would help you see it in a new way?")
PILOT_OTHER = "Anything else? For example, something confusing, too long, too obvious, or missing."
# ---- end of the pilot settings ----

API_TRIES = 5                # a model call that fails because the service is busy is tried this many times

FREE_QUESTION = "What stood out to you when you did the activity this way? What did you notice?"
GENERAL_QUESTION = ("Apart from these, did you find other ways you engaged more in the activity, "
                    "or learn anything else (from anyone or anywhere)?")


# =============================================================================
# PROMPTS. Rule kept from v5: the app knows no activity. No example in a prompt may come from a
# particular activity; if results are weak on one, fix the general wording.
# =============================================================================
PROMPT_INTAKE = """Someone was asked: "{question}"
They answered: \"\"\"{answer}\"\"\"
{qa}
Read what they wrote. Use only what they said; don't fill gaps with guesses.
- "activity": the activity, in a few plain words, the way they'd name it.
- "knowledge": what they already do, focus on, or understand in the activity, one short line each, in their words where possible.
- "not_enough": what they say they aren't doing enough of, one short line each.
- "level": a modest guess at their overall experience in the activity, 1-10 (1 = just started; 10 = many years, very skilled).
- "level_why": one sentence on what the guess is based on.
- "misc": details about them that are neither knowledge nor experience but could change what suits them: their body, how often and where and with whom they do it, equipment, worries, preferences. One short line each; [] if none.
- "questions": {questions_rule}"""

QUESTIONS_RULE = ("if you can't tell how long and how often they've done the activity, or something else you'd "
                  "need in order to suggest what they should try next, up to {max_q} short questions (under "
                  "{words} words each), specific to this activity where that helps. Never about technique. "
                  "Otherwise [].")
QUESTIONS_DONE = "[] (they've already answered your questions)."

PROMPT_INTAKE_FORMAT = """

Return only JSON:
{"activity": "...", "knowledge": ["..."], "not_enough": ["..."], "level": 3, "level_why": "...", "misc": ["..."], "questions": ["..."]}"""

NAMING_RULE = """

Naming actions: when an action is a standard part of {activity}, one that people who do {activity} call by a name, use that name. Put it in the action's "name" (as a short instruction built around it, like "Set a X" or "Use the X when ...") and in "called". Don't describe a well-known thing in roundabout words when it has a name: the person will hear the name from others and needs to recognise it. Describe in plain words only when the action has no common name, and never invent a name."""

PROMPT_TREE = """You have done {activity} for many years. Build a map of {activity} as a tree, used to choose what a newer person could try next.

Level 1, dialectical values: {v_min}-{v_max} of the broadest values of {activity}. These can't simply be taught. Someone new who is told to focus on one would have no idea where to begin or what it even consists of; someone experienced understands it as the sum of a great deal of time spent doing {activity}. Each is learned by doing {activity} itself.

Level 2, principles: under each value, {p_min}-{p_max} principles specific to {activity}: the parts of that value tied to {activity}'s own situations. Less abstract than the value, but still not something a newcomer understands just by being told to focus on it.

Level 3, actions: under each principle, {a_min}-{a_max} concrete actions someone who has been doing {activity} for a while could be told to do the next time they do {activity}: specific, usually with a name people in {activity} use, explainable in a sentence or two, with a reason that makes sense to someone still learning {activity}. Not a general instruction (be aware, communicate more, relax) and not a drill done apart from {activity}.

The same action may belong under more than one principle, even under different values. When it does, give it one id and list that id under each principle.
Be selective, not exhaustive: this is not a list of every way of acting in {activity}. Leave out the obvious basics that people pick up on their own within their first few times, and anything most people would think of if asked "what should I work on?". Prefer the less obvious actions: the ones an experienced person knows and does without thinking, that people still learning rarely come up with themselves, and that change how {activity} goes once someone does them.

For every action give two difficulty ratings from 1 to 10:
- "overall": how much general experience in {activity} it needs (1 = anyone can do it on their first day; 10 = only very experienced people, or physically out of reach for most).
- "principle": how much of the other actions under its principle(s) the person must already know (1 = none of them; 10 = it combines many of them).
The two can differ: an action can be easy to physically do but need a lot of its principle, or the reverse.

Also give every action an "impact" rating from 1 to 10: how much doing it changes how {activity} goes, for the person's own goals and for whoever they do it with (1 = a small refinement only they would notice; 10 = it changes what happens around them and opens up many other things). Of two actions in the same principle, the one that creates chances for others, changes what others have to respond to, or is a building block for many other actions has the higher impact than one that only improves the person's own small part. Spread the impact ratings across the whole range: across the map, roughly a third of the actions should be 1-4, a third 5-6 and a third 7-10. A high rating means the action really stands out from the others, not that it is useful (every action on the map is useful).""" + NAMING_RULE

PROMPT_TREE_FORMAT = """

Return only JSON:
{"values": [{"name": "...", "what": "one sentence", "principles": [{"name": "...", "what": "one sentence", "actions": ["a1", "a2", "a3"]}]}],
 "actions": [{"id": "a1", "name": "the action, as a short instruction, built around its common name if it has one", "called": "the name people in the activity use for it, or \\"\\"", "what": "one plain sentence on what it is", "why": "one sentence on why it helps, in a newcomer's terms", "overall": 2, "principle": 3, "impact": 6}]}"""

PROMPT_UPDATE = """This is a map of {activity}, what we know about one person who is getting into it, and something new they've written.

MAP (ids, current state, difficulty: overall / principle, and impact):
{tree}

WHAT WE KNOW ABOUT THEM:
{profile}

NEW, IN THEIR WORDS:
{new}

Update what we know. Use only what the new text shows.
- "done": ids of actions the new text shows they already do, or understand well enough that suggesting it would tell them nothing new. Something they successfully did this time counts. Also count actions so basic for their level that anyone who does {activity} as they describe would already know them. An action under several principles is one id.
- "partial": ids of actions the text suggests they touch on without showing it clearly (for example they mention the general thing but not which form of it).
- "new_actions": things they do or noticed that the map doesn't have, written as actions and placed under one or more existing principle ids, with "state": "done" when they now do it (or "open" when it's a thing they could try), both difficulty ratings, and an impact rating (1-10, as in the map).
- "knowledge": new short lines for what they do, understand or noticed, in their words where possible.
- "level": their overall experience in {activity} now, 1-10, or null if this doesn't change it. Move it by at most 1.
- "principle_levels": only for principles where what they wrote shows they know more (or less) than their overall level: {"p3": 5}. {} if none.
- "misc": new details about them that are neither knowledge nor experience but could change what suits them. [] if none.""" + NAMING_RULE

PROMPT_UPDATE_FORMAT = """

Return only JSON:
{"done": ["a1"], "partial": ["a2"], "new_actions": [{"name": "...", "called": "", "what": "...", "why": "...", "principles": ["p1"], "state": "done", "overall": 2, "principle": 2, "impact": 5}],
 "knowledge": ["..."], "level": null, "principle_levels": {}, "misc": ["..."]}"""

PROMPT_MISS = """Think of people doing {activity} at about this level of experience: {level} out of 10 ({level_word}).
What people like this already do or understand: {knowledge}
What do people like this usually miss, or think about least, in {activity}? Not the standard advice they have already heard many times, but the less obvious parts that experienced people notice and people at this level rarely think of. Give {n}, most important first, each one line: the part of {activity}, then in a few words why people at this level miss it."""

PROMPT_MISS_FORMAT = """

Return only JSON: {"misses": ["...", "..."]}"""

PROMPT_LOCATE = """These are the principles of {activity}:
{principles}

These are things people at a certain level usually miss in {activity}:
{misses}

For each numbered item, give the id of the principle it belongs to most, or null if none fits."""

PROMPT_LOCATE_FORMAT = """

Return only JSON: {"located": [{"item": 1, "principle": "p3"}]}"""

PROMPT_CHOOSE = """Someone is getting into {activity}. What we know about them:
{profile}

These actions come from the parts of {activity} they engage with least, and are within reach of their experience:
{shortlist}

Choose {n} for them to try the next time they do {activity}. Prefer high-impact actions: ones that change how {activity} goes for them and the people they do it with, so they see that what they are doing does a lot of good for their goals and for {activity} itself. Among actions of similar impact, prefer the less obvious ones, ones they are unlikely to have thought of or been told already; assume they know a bit more about {activity} than what we know about them shows. Prefer actions that are a small stretch rather than none, that suit their particular details (body, setting, who they do it with), and that they can do in the way they actually do {activity}. Two from the same principle are fine when they work together.

For each one you choose, also give a "moment": one line naming a specific moment, in the way they actually do {activity}, where that action is the thing to do: where they are, who or what is around them, what just happened. Each one's card will be set in its moment, so the moments must be clearly different from each other{taken}."""

PROMPT_CHOOSE_FORMAT = """

Return only JSON: {"chosen": [{"id": "a7", "why": "one sentence for the researcher's log: why this action, for this person", "moment": "one line"}]}"""

PROMPT_EXPAND = """You have done {activity} for years, and you're helping a friend who's newer to it.
What you know about them:
{profile}

This time you're giving them these to try:
{items}

Write up {this}{others} so they could do it the next time they do {activity} without asking you anything: plain words, short sentences, one idea per line. The only special word on a card is the action's own name, if it has one, explained once in one sentence.
- "title": the action, as a short instruction (under 10 words). If the action has a name people use, the title uses that name ("Set a X ..."), not a description of it; otherwise plain words.
- "called": if the action has a name people use, one sentence: "This is called a X: ..." in plain words. Otherwise "".
- "scene": one specific moment where they could do it, the way they actually do {activity}: start with "Imagine you're", then 2-4 short lines on where they are, what's around them (other people by their role in that moment, never by an invented name; or, alone, what's in front of them) and what just happened. When a moment is given with the action, the scene is that moment, and it must not read like the moments given with the other actions.
- "normally": "Normally, you might ..." (what someone at their level would do in that moment).
- "steps": 3-5 short numbered steps that refer to the scene, each easy to remember in the middle of {activity}.
- "key": one sentence on the single thing that's different from what they'd normally do.
- "why": why it works, 2-3 short lines: what's going on in {activity} and what changes when they do this, including what it does for their goals and for whoever they do it with, so they could work it out themselves. Use their details where they matter.
- "fits": one sentence on the bigger part of {activity} this is a piece of, in plain words, so they see where it leads.
- "enough": "You don't need to ... for this to count." It's about trying it, not succeeding.
- "experiment": 2 other moments to try it in, each starting "Try it when ...".
- "watch": one line on when not to do it, or what commonly goes wrong; or "".
Don't tell them what they'll feel, enjoy or value, and don't promise they'll improve. No counts, no reps, nothing to finish."""

PROMPT_EXPAND_FORMAT = """

Return only JSON:
{"cards": [{"id": "the id of the one you are writing up", "title": "...", "called": "", "scene": ["Imagine you're ...", "..."], "normally": "Normally, you might ...", "steps": ["..."], "key": "...", "why": ["..."], "fits": "...", "enough": "You don't need to ...", "experiment": ["Try it when ..."], "watch": ""}]}"""

PROMPT_GUIDED = """Someone getting into {activity} tried these and wrote what stood out to them:
{items}

For each one, write 1-2 short questions about specific things they may have noticed in the moment they tried it: where the people or things around them were, what happened just before or just after, what changed. Each question should lead them to think about the bigger part of {activity} the action belongs to (given in brackets), without naming it or telling them what they should have noticed. Concrete, answerable from memory, not yes/no, and not repeating what they already wrote."""

PROMPT_GUIDED_FORMAT = """

Return only JSON: {"questions": [{"id": "a3", "questions": ["...", "..."]}]}"""

PROMPT_ASSESS = """Someone getting into {activity} was asked to try some things and then wrote back. What we know about them:
{profile}

{items}

For each [id], decide:
- "outcome": "engaged" (they did it, roughly as meant), "misinterpreted" (they did something else instead: an action they took to be the same, or kept doing what they already focus on), "afraid" (they held back out of fear or discomfort), "no_chance" (the situation didn't come up), or "other" (they didn't try it for another reason).
- "note": one sentence for the researcher, quoting or paraphrasing what shows it.
- "reply": what to say to them, by outcome:
  misinterpreted: say plainly what the action was and how it differs from what they did, explain why it was suggested for them (use what we know about them), ask one specific question about the moment they tried it, and ask them to try it again.
  afraid: first say the worry makes sense, then explain why this action suits them (use what we know about them) and what makes it easier or safer, and ask them to try it again.
  no_chance: one line on when the situation tends to come up, and to try it then.
  other: one line acknowledging what got in the way, and asking them to try it again.
  engaged: "".
  Speak to them directly, warmly and briefly (2-4 sentences)."""

PROMPT_ASSESS_FORMAT = """

Return only JSON: {"assessed": [{"id": "a3", "outcome": "engaged", "note": "...", "reply": ""}]}"""

PROMPT_BACKTRACK = """Someone getting into {activity} has tried this {k} times without managing it: {action}
What they wrote each time:
{history}

What we know about them:
{profile}

The principles of {activity} (id, value, name):
{principles}

What is the underlying thing in the way? Name it as a principle of {activity}: use an existing principle id if one fits; otherwise make a new principle and give the id of the value it belongs under. Then give 2-3 easier actions under it that build toward the one they're stuck on, each with lower difficulty ratings than it, and each something they could do the next time they do {activity}.""" + NAMING_RULE

PROMPT_BACKTRACK_FORMAT = """

Return only JSON:
{"principle": {"id": "p4 or null", "name": "...", "what": "...", "value": "v2"}, "why": "one sentence for the researcher",
 "actions": [{"name": "...", "called": "", "what": "...", "why": "...", "overall": 1, "principle": 2, "impact": 5}]}"""


# =============================================================================
# MODEL BACKENDS (from v5): each .complete() is one stateless call
# =============================================================================
class ClaudeCodeBackend:
    """Each prompt goes through the `claude` command and the subscription you're logged into.
    For testing on your own laptop only (a personal plan can't serve other people)."""

    def __init__(self, model: str):
        self.model = model

    def complete(self, prompt: str, step: str = "", max_tokens: int = 0) -> str:
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("CLAUDECODE", None)
        env["MAX_THINKING_TOKENS"] = "0"
        result = subprocess.run(
            ["claude", "-p", "--model", self.model, "--tools", "", "--system-prompt", "You are a helpful assistant.",
             "--strict-mcp-config", "--no-session-persistence"],
            input=prompt, capture_output=True, text=True, env=env, cwd=tempfile.gettempdir())
        if result.returncode != 0:
            raise RuntimeError(f"{step}: claude command failed:\n{result.stderr or result.stdout}")
        return result.stdout


class ClaudeAPIBackend:
    """The Claude API, paid per use from the API account's credits. The backend for serving other people.
    Reads ANTHROPIC_API_KEY. Streams so long replies (the tree) can't hit an HTTP timeout."""

    def __init__(self, model: str, effort: str = "medium"):
        import anthropic
        self.client = anthropic.Anthropic()
        self.model, self.effort = model, effort
        self.input_tokens = self.output_tokens = 0   # running totals, for cost checks

    def complete(self, prompt: str, step: str = "", max_tokens: int = 32000) -> str:
        for attempt in range(1, API_TRIES + 1):
            try:
                msg = self._once(prompt, max_tokens)
                break
            except Exception as e:   # busy, rate-limited or a dropped connection: wait and try again
                if attempt == API_TRIES or not self._worth_retrying(e):
                    raise
                wait = min(30, 2 ** attempt) + random.random()
                print(f"  [warn] {step}: {type(e).__name__}; trying again in {wait:.0f}s "
                      f"(attempt {attempt} of {API_TRIES})", file=sys.stderr)
                time.sleep(wait)
        self.input_tokens += msg.usage.input_tokens
        self.output_tokens += msg.usage.output_tokens
        if msg.stop_reason == "refusal":
            raise RuntimeError(f"{step}: the model declined this request")
        if msg.stop_reason == "max_tokens":
            print(f"  [warn] {step}: reply hit max_tokens and was cut off", file=sys.stderr)
        return "".join(b.text for b in msg.content if b.type == "text")

    def _once(self, prompt: str, max_tokens: int):
        with self.client.beta.messages.stream(
            model=self.model, max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": self.effort},
            # if a safety classifier declines, the API retries on its default fallback model in the same call
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        ) as stream:
            return stream.get_final_message()

    @staticmethod
    def _worth_retrying(e: Exception) -> bool:
        """True for trouble that passes (too many requests, overloaded, server error, dropped connection);
        False for a request that would fail again (bad key, bad request)."""
        import anthropic
        if isinstance(e, anthropic.APIStatusError):
            body = e.body if isinstance(e.body, dict) else {}
            kind = (body.get("error") or {}).get("type") if isinstance(body.get("error"), dict) else ""
            return (e.status_code in (408, 409, 429) or e.status_code >= 500
                    or kind in ("overloaded_error", "rate_limit_error", "api_error", "timeout_error"))
        if isinstance(e, anthropic.AnthropicError):
            return isinstance(e, anthropic.APIConnectionError)
        return isinstance(e, OSError) or type(e).__module__.split(".")[0].startswith("http")   # the connection itself


class LocalBackend:
    """Any OpenAI-compatible local server (Ollama: http://localhost:11434/v1)."""

    def __init__(self, model: str, base_url: str):
        from openai import OpenAI
        self.client = OpenAI(base_url=base_url, api_key=os.getenv("LOCAL_API_KEY", "not-needed"))
        self.model = model

    def complete(self, prompt: str, step: str = "", max_tokens: int = 12000) -> str:
        resp = self.client.chat.completions.create(
            model=self.model, messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens, temperature=0.7,
            reasoning_effort="none")  # Qwen's "thinking" off: much faster
        return resp.choices[0].message.content or ""


def make_backend(kind: str, model: str | None = None):
    if kind == "api":
        return ClaudeAPIBackend(model or os.getenv("API_MODEL", "claude-sonnet-5-5"),
                                os.getenv("API_EFFORT", "medium"))
    if kind == "claude":
        return ClaudeCodeBackend(model or os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5"))
    if kind == "local":
        return LocalBackend(model or os.getenv("LOCAL_MODEL", "qwen3.5:9b"),
                            os.getenv("LOCAL_BASE_URL", "http://localhost:11434/v1"))
    if kind == "mock":
        from mock_backend import MockBackend
        return MockBackend()
    raise ValueError(f"unknown backend: {kind}")


# =============================================================================
# HELPERS
# =============================================================================
def fill(template: str, **variables) -> str:
    """Replace only the {names} passed in, so JSON examples in prompts need no escaping."""
    return re.sub(r"\{(\w+)\}", lambda m: str(variables[m.group(1)]) if m.group(1) in variables else m.group(0),
                  template)


def strip_thinking(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


def repair_json(text: str) -> str:
    text = re.sub(r'([{,]\s*)"{2,}(?=\w)', r'\1"', text)
    text = re.sub(r'(?<=\w)"{2,}(\s*:)', r'"\1', text)
    return re.sub(r",(\s*[}\]])", r"\1", text)


def parse_json(text: str):
    text = strip_thinking(text)
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        raise ValueError("no JSON found")
    text = text[min(starts):]
    decode = json.JSONDecoder().raw_decode
    try:
        return decode(text)[0]
    except json.JSONDecodeError:
        return decode(repair_json(text))[0]


def ask_json(backend, j: dict | None, prompt: str, step: str, retries: int = 1) -> dict:
    """Call the model, parse JSON (asking once more if it isn't), and log the call in the journal."""
    raw = ""
    for attempt in range(retries + 1):
        p = prompt if attempt == 0 else prompt + "\n\nReturn ONLY valid JSON. No prose, no code fences."
        raw = backend.complete(p, step=step)
        try:
            data = parse_json(raw)
            if j is not None:
                j.setdefault("calls", []).append({"step": step, "time": now(), "prompt": p, "raw": raw})
            return data if isinstance(data, dict) else {"items": data}
        except (ValueError, json.JSONDecodeError):
            print(f"  [warn] {step}: couldn't parse JSON (attempt {attempt + 1})", file=sys.stderr)
    raise RuntimeError(f"{step}: the model never returned valid JSON. Last reply:\n{raw}")


def log_calls(j: dict, calls: list[dict]) -> None:
    """Add calls made off to the side (in a background thread) to the journal's log, kept in time order."""
    j.setdefault("calls", []).extend(calls)
    j["calls"].sort(key=lambda c: c["time"])


def now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "activity"


def as_list(x) -> list[str]:
    if isinstance(x, str):
        x = [x]
    return [str(s).strip() for s in (x or []) if str(s).strip()]


def as_level(x, default=None):
    try:
        return max(1, min(10, int(round(float(x)))))
    except (TypeError, ValueError):
        return default


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", (s or "").lower()).strip()


def level_word(n: int) -> str:
    return "new" if n <= 2 else "still fairly new" if n <= 4 else "intermediate" if n <= 6 else "experienced"


# =============================================================================
# THE TREE
# j["tree"] = {"values": {vid: {...,"principles":[pid]}}, "principles": {pid: {...,"value": vid,"actions":[aid]}},
#              "actions": {aid: {..., "state", "overall", "principle", "impact", "attempts", "blocked_by", "source"}}}
# =============================================================================
def empty_tree() -> dict:
    return {"values": {}, "principles": {}, "actions": {}}


def next_id(d: dict, prefix: str) -> str:
    n = max([int(k[1:]) for k in d if re.fullmatch(rf"{prefix}\d+", k)] + [0]) + 1
    return f"{prefix}{n}"


def new_action(tree: dict, raw: dict, source: str, state: str = "open") -> str | None:
    """Add an action, or return the id of an existing one with the same name (actions are shared, not copied)."""
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    for aid, a in tree["actions"].items():
        if norm(a["name"]) == norm(name):
            return aid
    aid = next_id(tree["actions"], "a")
    tree["actions"][aid] = {
        "name": name, "called": str(raw.get("called") or "").strip(), "what": str(raw.get("what") or "").strip(),
        "why": str(raw.get("why") or "").strip(), "overall": as_level(raw.get("overall"), 3),
        "principle": as_level(raw.get("principle"), 3), "impact": as_level(raw.get("impact"), 5), "state": state if state in STATES else "open",
        "attempts": 0, "blocked_by": [], "source": source, "suggested": [],
    }
    return aid


def attach(tree: dict, aid: str, pid: str) -> None:
    p = tree["principles"].get(pid)
    if p is not None and aid not in p["actions"]:
        p["actions"].append(aid)


def build_tree(data: dict) -> tuple[dict, list[str]]:
    """Turn PROMPT_TREE's reply into the tree. Returns (tree, problems)."""
    tree, problems = empty_tree(), []
    raw_actions = {str(a.get("id")): a for a in data.get("actions", []) if isinstance(a, dict) and a.get("id")}
    id_map: dict[str, str] = {}
    for rid, ra in raw_actions.items():
        aid = new_action(tree, ra, "tree")
        if aid:
            id_map[rid] = aid
    for v in data.get("values", []):
        if not isinstance(v, dict) or not v.get("name"):
            continue
        vid = next_id(tree["values"], "v")
        tree["values"][vid] = {"name": str(v["name"]).strip(), "what": str(v.get("what") or "").strip(), "principles": []}
        for p in v.get("principles", []):
            if not isinstance(p, dict) or not p.get("name"):
                continue
            pid = next_id(tree["principles"], "p")
            tree["principles"][pid] = {"name": str(p["name"]).strip(), "what": str(p.get("what") or "").strip(),
                                       "value": vid, "actions": [], "source": "tree"}
            tree["values"][vid]["principles"].append(pid)
            for rid in p.get("actions", []):
                rid = rid.get("id") if isinstance(rid, dict) else str(rid)
                if rid in id_map:
                    attach(tree, id_map[rid], pid)
                else:
                    problems.append(f"principle {p['name']!r} lists unknown action {rid!r}")
    orphans = [aid for aid in tree["actions"] if not parents(tree, aid)]
    for aid in orphans:
        del tree["actions"][aid]
    if orphans:
        problems.append(f"{len(orphans)} actions weren't under any principle (dropped)")
    nv = len(tree["values"])
    if not VALUES_RANGE[0] <= nv <= VALUES_RANGE[1]:
        problems.append(f"{nv} values (asked for {VALUES_RANGE[0]}-{VALUES_RANGE[1]})")
    thin = [p["name"] for p in tree["principles"].values() if len(p["actions"]) < ACTIONS_RANGE[0]]
    if thin:
        problems.append(f"principles with fewer than {ACTIONS_RANGE[0]} actions: {', '.join(thin)}")
    return tree, problems


def parents(tree: dict, aid: str) -> list[str]:
    return [pid for pid, p in tree["principles"].items() if aid in p["actions"]]


def tree_text(tree: dict, with_state: bool = True) -> str:
    """The tree as compact text for prompts and the researcher view."""
    lines = []
    for vid, v in tree["values"].items():
        lines.append(f"VALUE {vid}: {v['name']}")
        for pid in v["principles"]:
            p = tree["principles"][pid]
            lines.append(f"  PRINCIPLE {pid}: {p['name']}")
            for aid in p["actions"]:
                a = tree["actions"][aid]
                state = f" [{a['state']}]" if with_state else ""
                shared = [x for x in parents(tree, aid) if x != pid]
                also = f" (also under {', '.join(shared)})" if shared else ""
                lines.append(f"    {aid}{state} {a['name']} ({a['overall']}/{a['principle']}, impact {a.get('impact', 5)}){also}")
    return "\n".join(lines)


def principles_text(tree: dict) -> str:
    return "\n".join(f"{pid} ({tree['values'][p['value']]['name']}): {p['name']}" for pid, p in tree["principles"].items())


# =============================================================================
# THE PROFILE (outside the tree)
# =============================================================================
def empty_profile() -> dict:
    return {"level": 2, "level_why": "", "level_history": [], "principle_levels": {}, "knowledge": [],
            "not_enough": [], "misc": []}


def profile_text(j: dict) -> str:
    pr, tree = j["profile"], j.get("tree") or empty_tree()
    lines = [f"Overall experience: {pr['level']}/10 ({level_word(pr['level'])}). {pr.get('level_why', '')}".strip()]
    for pid, lv in pr["principle_levels"].items():
        if pid in tree["principles"]:
            lines.append(f"Experience in {tree['principles'][pid]['name']} ({pid}): {lv}/10")
    if pr["knowledge"]:
        lines.append("What they already do or understand: " + "; ".join(pr["knowledge"]))
    if pr["not_enough"]:
        lines.append("What they say they aren't doing enough: " + "; ".join(pr["not_enough"]))
    if pr["misc"]:
        lines.append("Other details about them: " + "; ".join(pr["misc"]))
    return "\n".join(lines)


def principle_level(j: dict, pid: str) -> int:
    return j["profile"]["principle_levels"].get(pid, j["profile"]["level"])


def add_unique(lst: list[str], items) -> list[str]:
    added = []
    for s in as_list(items):
        if norm(s) not in {norm(x) for x in lst}:
            lst.append(s)
            added.append(s)
    return added


# =============================================================================
# JOURNALS
# =============================================================================
def journals_dir() -> Path:
    return Path(os.getenv("JOURNALS_DIR", "journals"))


# Where journals are stored: files under journals_dir() by default, or one Postgres table when DATABASE_URL
# is set (any Postgres: Supabase, Neon, local). A journal holds everything: what the person wrote, the tree,
# the profile, and every prompt and raw reply (`calls`), so storing journals stores the full logs.
DB_SCHEMA = """
create table if not exists dtr_journals (
    slug     text primary key,
    key      text unique,
    activity text,
    updated  timestamptz not null default now(),
    data     jsonb not null
);
-- Supabase serves the public schema over its web API; RLS with no policies blocks that path.
-- The app connects as the table owner, which RLS doesn't restrict.
alter table dtr_journals enable row level security"""
_schema_ready: set[str] = set()


def database_url() -> str:
    return os.getenv("DATABASE_URL", "").strip()


def _db():
    """A fresh connection (closed by `with`). prepare_threshold=None keeps it working through poolers."""
    import psycopg
    url = database_url()
    conn = psycopg.connect(url, autocommit=True, prepare_threshold=None, connect_timeout=15)
    if url not in _schema_ready:
        conn.execute(DB_SCHEMA)
        _schema_ready.add(url)
    return conn


_conn = None                    # (url, connection) kept open between statements
_conn_lock = threading.Lock()   # the page runs each visitor in its own thread


def _query(sql: str, params=(), fetch: bool = False):
    """Run one statement on a connection that stays open: connecting takes several times longer than the
    statement itself, and the page loads the journal on every click. If the connection has gone stale
    (the server closed it while idle), reconnect once and run the statement again; every statement here
    is safe to repeat."""
    global _conn
    import psycopg
    url = database_url()
    with _conn_lock:
        for attempt in (1, 2):
            try:
                if _conn is None or _conn[0] != url or _conn[1].closed:
                    if _conn is not None:
                        _conn[1].close()
                    _conn = (url, _db())
                cur = _conn[1].execute(sql, params)
                return cur.fetchall() if fetch else None
            except (psycopg.OperationalError, psycopg.InterfaceError):
                if _conn is not None:
                    try:
                        _conn[1].close()
                    except Exception:
                        pass
                _conn = None
                if attempt == 2:
                    raise


def _jsonb(j: dict):
    from psycopg.types.json import Jsonb
    # Postgres jsonb can't hold the NUL character; a model reply could in principle contain one
    return Jsonb(j, dumps=lambda o: json.dumps(o, ensure_ascii=False).replace("\\u0000", ""))


def save(j: dict) -> None:
    j["updated"] = now()
    if database_url():
        _query("insert into dtr_journals (slug, key, activity, data, updated) values (%s, %s, %s, %s, now()) "
               "on conflict (slug) do update set key = excluded.key, activity = excluded.activity, "
               "data = excluded.data, updated = now()",
               (j["slug"], j.get("key"), j.get("activity"), _jsonb(j)))
        return
    d = journals_dir() / j["slug"]
    d.mkdir(parents=True, exist_ok=True)
    (d / "journal.json").write_text(json.dumps(j, indent=2, ensure_ascii=False))
    if j.get("tree"):
        (d / "tree.md").write_text(f"# {j['activity']}\n\n```\n{tree_text(j['tree'])}\n```\n\n## Profile\n\n"
                                   f"{profile_text(j)}\n")


def load(slug: str) -> dict:
    if database_url():
        rows = _query("select data from dtr_journals where slug = %s", (slug,), fetch=True)
        if not rows:
            raise FileNotFoundError(f"no journal {slug!r}")
        return rows[0][0]
    return json.loads((journals_dir() / slug / "journal.json").read_text())


def list_journals() -> list[str]:
    """Slugs, most recently updated first."""
    if database_url():
        return [r[0] for r in _query("select slug from dtr_journals order by updated desc", fetch=True)]
    d = journals_dir()
    if not d.exists():
        return []
    return sorted((p.name for p in d.iterdir() if (p / "journal.json").exists()),
                  key=lambda s: (journals_dir() / s / "journal.json").stat().st_mtime, reverse=True)


def find_by_key(key: str) -> str | None:
    """The slug of the journal whose private key is `key`, or None."""
    if not key:
        return None
    if database_url():
        rows = _query("select slug from dtr_journals where key = %s", (key,), fetch=True)
        return rows[0][0] if rows else None
    for slug in list_journals():
        try:
            if secrets.compare_digest(str(load(slug).get("key") or ""), key):
                return slug
        except (OSError, json.JSONDecodeError):
            continue
    return None


def export_all() -> list[dict]:
    """Every journal in full (answers, cards, feedback, profile, tree, every prompt and reply), newest first."""
    out = []
    for slug in list_journals():
        try:
            out.append(load(slug))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def unique_slug(activity: str) -> str:
    base = slugify(activity)
    if database_url():   # many people at once: a random suffix, so two journals can never share a slug
        return f"{base}-{secrets.token_hex(3)}"
    slug, n = base, 2
    while (journals_dir() / slug).exists():
        slug, n = f"{base}-{n}", n + 1
    return slug


# =============================================================================
# STEPS 1-3: intake, follow-up questions, the tree
# =============================================================================
def intake(backend, answer: str, qa: list[dict] | None = None, j: dict | None = None) -> dict:
    """Read the first answer (and answers to follow-up questions). Returns PROMPT_INTAKE's fields."""
    qa_text = ""
    if qa:
        qa_text = "They were then asked:\n" + "\n".join(f'Q: {x["q"]}\nA: {x["a"] or "(no answer)"}' for x in qa)
    rule = QUESTIONS_DONE if qa is not None else fill(QUESTIONS_RULE, max_q=MAX_QUESTIONS, words=QUESTION_WORDS)
    prompt = fill(PROMPT_INTAKE, question=SEARCH_PROMPT, answer=answer, qa=qa_text, questions_rule=rule)
    data = ask_json(backend, j, prompt + PROMPT_INTAKE_FORMAT, "intake")
    data["questions"] = [] if qa is not None else [
        q for q in as_list(data.get("questions")) if len(q.split()) <= QUESTION_WORDS][:MAX_QUESTIONS]
    return data


def start(backend, answer: str) -> dict:
    """Step 1-2: a new journal from the first answer. If the model has questions, j["questions"] is set and
    the caller asks them, then calls answer_questions(); otherwise the tree is built right away."""
    j = {"version": 6, "created": now(), "answer": answer, "qa": [], "profile": empty_profile(), "tree": None,
         "rounds": [], "calls": [], "boost": [], "backend": getattr(backend, "model", ""),
         "key": secrets.token_urlsafe(16),   # the private link to this journal (?j=key); the slug is guessable
         "mode": MODE}                       # PILOT (temporary): which flow this journal follows
    data = intake(backend, answer, j=j)
    j["activity"] = str(data.get("activity") or "").strip() or "the activity"
    j["slug"] = unique_slug(j["activity"])
    apply_intake(j, data)
    j["questions"] = data["questions"]
    if not j["questions"]:
        build(backend, j)
    elif PREFETCH_TREE:
        prefetch_tree(backend, j)   # the tree needs only the activity: build it while they answer
    save(j)
    return j


def apply_intake(j: dict, data: dict) -> None:
    pr = j["profile"]
    pr["level"] = as_level(data.get("level"), pr["level"])
    pr["level_why"] = str(data.get("level_why") or pr.get("level_why") or "")
    add_unique(pr["knowledge"], data.get("knowledge"))
    add_unique(pr["not_enough"], data.get("not_enough"))
    add_unique(pr["misc"], data.get("misc"))


def answer_questions(backend, j: dict, answers: list[str]) -> dict:
    j["qa"] = [{"q": q, "a": (a or "").strip()} for q, a in zip(j.get("questions", []), answers)]
    data = intake(backend, j["answer"], qa=j["qa"], j=j)
    apply_intake(j, data)
    j["questions"] = []
    build(backend, j)
    save(j)
    return j


def fetch_tree(backend, activity: str) -> dict:
    """The tree call (asked again once if the reply is unusable). It reads and writes no journal, so it can
    run in a background thread; the calls it made come back to be logged by build()."""
    log = {"calls": []}
    prompt = fill(PROMPT_TREE, activity=activity, v_min=VALUES_RANGE[0], v_max=VALUES_RANGE[1],
                  p_min=PRINCIPLES_RANGE[0], p_max=PRINCIPLES_RANGE[1], a_min=ACTIONS_RANGE[0], a_max=ACTIONS_RANGE[1])
    tree, problems = build_tree(ask_json(backend, log, prompt + PROMPT_TREE_FORMAT, "tree"))
    if problems and (not tree["values"] or len(tree["actions"]) < 6):
        note = "\n\nYour last reply had these problems; fix them: " + "; ".join(problems)
        tree, problems = build_tree(ask_json(backend, log, prompt + note + PROMPT_TREE_FORMAT, "tree"))
    return {"tree": tree, "problems": problems, "calls": log["calls"]}


# Trees built ahead, misses, cards. Each worker only waits on the model, so many are cheap; 64 covers about
# 15 people at the same step at once (5 calls each at the busiest), and anything beyond that waits its turn.
_pool = ThreadPoolExecutor(max_workers=64, thread_name_prefix="dtr")
_trees: dict[str, Future] = {}   # journal key -> its tree, being built ahead of time
_TREES_KEPT = 200                # trees of people who never came back are dropped, oldest first


def prefetch_tree(backend, j: dict) -> None:
    """Start the tree in the background. build() picks it up; if the server restarted in between, or the
    call failed, build() simply asks for the tree itself, as before."""
    while len(_trees) >= _TREES_KEPT:
        _trees.pop(next(iter(_trees))).cancel()
    _trees[j["key"]] = _pool.submit(fetch_tree, backend, j["activity"])


def build(backend, j: dict) -> None:
    """Step 3: the tree, then the profile located on it, then the first round."""
    asked = start_miss(backend, j)      # needs only the profile, so it starts before the tree is in
    got, ahead = None, _trees.pop(j.get("key"), None)
    if ahead is not None:
        try:
            got = ahead.result()    # waits for whatever is left of the call
        except Exception as e:      # the early call failed: ask again now
            print(f"  [warn] tree built ahead failed ({type(e).__name__}); asking again", file=sys.stderr)
    if got is None:
        got = fetch_tree(backend, j["activity"])
    tree, problems = got["tree"], got["problems"]
    log_calls(j, got["calls"])
    if not tree["actions"]:
        raise RuntimeError("the model didn't return a usable tree: " + "; ".join(problems))
    j["tree"], j["tree_problems"] = tree, problems
    first_words = j["answer"] + "".join(f"\nQ: {x['q']}\nA: {x['a']}" for x in j["qa"])
    ahead = start_misses(backend, j, asked)   # placed on the tree while the update runs
    j["intake_update"] = update(backend, j, first_words, "intake")
    plan_round(backend, j, ahead=ahead)


# =============================================================================
# UPDATING THE PROFILE AND THE TREE FROM WHAT THEY WROTE
# =============================================================================
def update(backend, j: dict, new_text: str, why: str) -> dict:
    """Locate what they wrote on the tree (cross off / downweight / add) and update the profile outside it.
    Returns a summary of what changed (for the page and the log)."""
    tree, pr = j["tree"], j["profile"]
    prompt = fill(PROMPT_UPDATE, activity=j["activity"], tree=tree_text(tree), profile=profile_text(j), new=new_text)
    data = ask_json(backend, j, prompt + PROMPT_UPDATE_FORMAT, f"update ({why})")
    changed = {"done": [], "partial": [], "added": [], "knowledge": [], "misc": [], "level": None, "principle_levels": {}}
    for aid in as_list(data.get("done")):
        a = tree["actions"].get(aid)
        if a and a["state"] != "done":
            a["state"] = "done"
            changed["done"].append(aid)
    for aid in as_list(data.get("partial")):
        a = tree["actions"].get(aid)
        if a and a["state"] == "open":
            a["state"] = "partial"
            changed["partial"].append(aid)
    for raw in data.get("new_actions") or []:
        if not isinstance(raw, dict):
            continue
        pids = [p for p in as_list(raw.get("principles")) if p in tree["principles"]]
        if not pids:
            continue
        aid = new_action(tree, raw, f"from what they wrote ({why})", str(raw.get("state") or "done"))
        if aid:
            for pid in pids:
                attach(tree, aid, pid)
            if raw.get("state") == "done" and tree["actions"][aid]["state"] != "done":
                tree["actions"][aid]["state"] = "done"
            changed["added"].append(aid)
    changed["knowledge"] = add_unique(pr["knowledge"], data.get("knowledge"))
    changed["misc"] = add_unique(pr["misc"], data.get("misc"))
    lv = as_level(data.get("level"))
    if lv is not None and lv != pr["level"]:
        lv = max(pr["level"] - 1, min(pr["level"] + 1, lv))
        pr["level_history"].append({"time": now(), "from": pr["level"], "to": lv, "why": why})
        pr["level"] = changed["level"] = lv
    for pid, v in (data.get("principle_levels") or {}).items():
        v = as_level(v)
        if pid in tree["principles"] and v is not None:
            pr["principle_levels"][pid] = v
            changed["principle_levels"][pid] = v
    return changed


# =============================================================================
# STEP 4: which principles are most underweighted
# =============================================================================
def coverage(j: dict, pid: str) -> float:
    acts = [j["tree"]["actions"][a] for a in j["tree"]["principles"][pid]["actions"]]
    if not acts:
        return 1.0
    got = sum(1.0 if a["state"] == "done" else PARTIAL_CREDIT if a["state"] == "partial" else 0.0 for a in acts)
    return got / len(acts)


def miss_weight(j: dict) -> float:
    """Heavy before any feedback; smaller with each closed round (the spec: less context, more weight)."""
    closed = sum(1 for r in j["rounds"] if r.get("closed"))
    return MISS_WEIGHT / (1 + closed)


def miss_prompt(j: dict) -> str:
    pr = j["profile"]
    return fill(PROMPT_MISS, activity=j["activity"], level=pr["level"], level_word=level_word(pr["level"]),
                knowledge="; ".join(pr["knowledge"]) or "(nothing said yet)", n=MISSES_ASKED) + PROMPT_MISS_FORMAT


def ask_miss(backend, prompt: str) -> dict:
    """The separate query: what do people like this usually miss? It is not shown the tree, so its answers
    can't lean on the tree's wording. Touches no journal (it runs in a background thread)."""
    log = {"calls": []}
    data = ask_json(backend, log, prompt, "miss")
    return {"misses": as_list(data.get("misses"))[:MISSES_ASKED], "calls": log["calls"]}


def locate_misses(backend, activity: str, principles: str, asked) -> dict:
    """Place the answers on the tree. `asked` is ask_miss()'s result, or the Future that will hold it."""
    got = asked.result() if isinstance(asked, Future) else asked
    out = {"misses": got["misses"], "located": [], "calls": list(got["calls"])}
    if got["misses"]:
        log = {"calls": []}
        prompt = fill(PROMPT_LOCATE, activity=activity, principles=principles,
                      misses="\n".join(f"{i}. {m}" for i, m in enumerate(got["misses"], 1)))
        located = ask_json(backend, log, prompt + PROMPT_LOCATE_FORMAT, "locate misses").get("located")
        out["located"] = located if isinstance(located, list) else []
        out["calls"] += log["calls"]
    return out


def start_miss(backend, j: dict) -> Future:
    """Ask the query in the background, from the profile as it is right now. Needs no tree."""
    return _pool.submit(ask_miss, backend, miss_prompt(j))


def start_misses(backend, j: dict, asked: Future | None = None) -> Future:
    """The query (unless `asked` is it, already running) and then its placing on the tree, in the
    background. plan_round() collects the result."""
    return _pool.submit(locate_misses, backend, j["activity"], principles_text(j["tree"]),
                        asked or start_miss(backend, j))


def miss_scores(j: dict, got: dict) -> dict[str, float]:
    """Principle id -> 0..1: how much of what people like this miss falls under it (earlier = more important)."""
    log_calls(j, got["calls"])
    misses = j["last_misses"] = got["misses"]
    scores: dict[str, float] = {}
    for item in got["located"]:
        if not isinstance(item, dict):
            continue
        pid, i = item.get("principle"), item.get("item")
        if pid in j["tree"]["principles"] and isinstance(i, int) and 1 <= i <= len(misses):
            scores[pid] = scores.get(pid, 0) + (len(misses) - i + 1) / len(misses)
    top = max(scores.values(), default=0) or 1
    return {pid: s / top for pid, s in scores.items()}


def score_principles(j: dict, misses: dict[str, float]) -> list[tuple[str, float]]:
    lam = miss_weight(j)
    out = []
    for pid in j["tree"]["principles"]:
        s = (1 - lam) * (1 - coverage(j, pid)) + lam * misses.get(pid, 0.0)
        if pid in j["boost"]:
            s += 1.0       # a backtrack put this first until its easier actions are done
        out.append((pid, round(s, 3)))
    return sorted(out, key=lambda x: -x[1])


# =============================================================================
# STEP 5: choosing 2-3 actions within reach
# =============================================================================
def available(j: dict, aid: str) -> bool:
    a = j["tree"]["actions"][aid]
    if a["state"] == "done":
        return False
    return all(j["tree"]["actions"].get(b, {}).get("state") == "done" for b in a.get("blocked_by", []))


def assumed_levels(j: dict, aid: str) -> tuple[int, int]:
    """(overall, principle) level to match actions against: the person's, plus AWARENESS."""
    p_level = max([principle_level(j, pid) for pid in parents(j["tree"], aid)] or [j["profile"]["level"]])
    return j["profile"]["level"] + AWARENESS, p_level + AWARENESS


def within_reach(j: dict, aid: str) -> bool:
    a = j["tree"]["actions"][aid]
    level, p_level = assumed_levels(j, aid)
    return a["overall"] <= level + STRETCH and a["principle"] <= p_level + STRETCH


def stretch_cost(j: dict, aid: str) -> float:
    """0 when the action is about one step above the person (plus AWARENESS); more the further from that."""
    a = j["tree"]["actions"][aid]
    level, p_level = assumed_levels(j, aid)
    return abs(a["overall"] - (level + 1)) + abs(a["principle"] - (p_level + 1))


def action_score(j: dict, aid: str, need: float) -> float:
    """Shortlist score, higher first: how much its principle needs attention, plus impact, less stretch."""
    a = j["tree"]["actions"][aid]
    return (need + IMPACT_WEIGHT * (a.get("impact", 5) - 5) / 5 - STRETCH_WEIGHT * stretch_cost(j, aid)
            - (PARTIAL_PENALTY if a["state"] == "partial" else 0))


def shortlist(j: dict, ranked: list[tuple[str, float]], exclude=()) -> list[tuple[str, str]]:
    """(action id, the principle it came through), within reach, best first. Every principle is considered:
    one the person engages with least counts most, but a high-impact action can outrank a low-impact one
    from a slightly more neglected principle."""
    best: dict[str, tuple[float, str]] = {}
    for pid, need in ranked:
        for a in j["tree"]["principles"][pid]["actions"]:
            if a in exclude or not available(j, a) or not within_reach(j, a):
                continue
            sc = action_score(j, a, need)
            if a not in best or sc > best[a][0]:   # a shared action counts through its neediest principle
                best[a] = (sc, pid)
    order = sorted(best.items(), key=lambda x: -x[1][0])
    return [(a, pid) for a, (_, pid) in order[:SHORTLIST]]


def choose(backend, j: dict, short: list[tuple[str, str]], n: int, taken=()) -> list[tuple[str, str, str, str]]:
    """The model picks n from the shortlist using the misc details, and gives each a moment for its card
    (different from each other and from `taken`, the moments of cards already in the round). Returns
    (action, principle, why, moment). Falls back to the top of the list."""
    if n <= 0 or not short:
        return []
    tree = j["tree"]
    lines = []
    for aid, pid in short:
        a = tree["actions"][aid]
        lines.append(f"[{aid}] {a['name']}: {a['what']} (part of {tree['principles'][pid]['name']}; "
                     f"needs experience {a['overall']}/10 overall, {a['principle']}/10 in that part; "
                     f"impact {a.get('impact', 5)}/10)")
    also = ("" if not taken else ", and from these moments, which they are already being given:\n"
            + "\n".join(f"- {m}" for m in taken))
    prompt = fill(PROMPT_CHOOSE, activity=j["activity"], profile=profile_text(j), shortlist="\n".join(lines), n=n,
                  taken=also)
    by_id = dict(short)
    picked = []
    try:
        for c in ask_json(backend, j, prompt + PROMPT_CHOOSE_FORMAT, "choose").get("chosen") or []:
            if isinstance(c, dict) and c.get("id") in by_id and c["id"] not in [p[0] for p in picked]:
                picked.append((c["id"], by_id[c["id"]], str(c.get("why") or ""), str(c.get("moment") or "").strip()))
    except RuntimeError:
        pass
    for aid, pid in short:
        if len(picked) >= n:
            break
        if aid not in [p[0] for p in picked]:
            picked.append((aid, pid, "top of the shortlist", ""))
    return picked[:n]


def last_scene(j: dict, aid: str) -> str:
    """The scene of the card this action had the last time it was given (a retry stays in that moment)."""
    for rnd in reversed(j["rounds"]):
        for c in rnd["cards"]:
            if c["action"] == aid:
                return " ".join(as_list(c["card"].get("scene"))) or c.get("moment", "")
    return ""


def expand(backend, j: dict, aids: list[str], moments: dict[str, str] | None = None) -> dict[str, dict]:
    """Write each action up as a card: one call per card, all at the same time. Every call is given the
    whole list of actions for this round, with the moment each card is set in, and writes one of them."""
    moments = moments or {}
    tree = j["tree"]
    items = []
    for aid in aids:
        a = tree["actions"][aid]
        called = f' (people call it "{a["called"]}")' if a.get("called") else ""
        moment = f"\n    Moment: {moments[aid]}" if moments.get(aid) else ""
        items.append(f"[{aid}] {a['name']}{called}: {a['what']} Why: {a['why']}{moment}")
    others = " only (the others are being written up separately)" if len(aids) > 1 else ""
    profile = profile_text(j)

    def one(aid: str) -> tuple[dict | None, list[dict]]:
        log, card = {"calls": []}, None
        try:
            prompt = fill(PROMPT_EXPAND, activity=j["activity"], profile=profile, items="\n".join(items),
                          this=f"[{aid}]", others=others)
            got = ask_json(backend, log, prompt + PROMPT_EXPAND_FORMAT, f"expand ({aid})")
            written = [c for c in got.get("cards") or [] if isinstance(c, dict)]
            card = next((c for c in written if c.get("id") == aid), written[0] if len(written) == 1 else None)
        except RuntimeError:
            pass
        return card, log["calls"]

    cards = {}
    for aid, (card, calls) in zip(aids, list(_pool.map(one, aids))):
        log_calls(j, calls)
        if card is None:   # a card that didn't come back keeps the tree's own short write-up
            a = tree["actions"][aid]
            card = {"title": a["name"], "called": f'This is called "{a["called"]}": {a["what"]}' if a.get("called") else "",
                    "steps": [a["what"]], "why": [a["why"]]}
        cards[aid] = {**card, "id": aid}
    return cards


def plan_round(backend, j: dict, retries: list[dict] | None = None, ahead: Future | None = None) -> dict:
    """Steps 4-5: score, shortlist, choose, write up. Retries (failed actions with a reply) go first.
    `ahead` is the "usually miss" query already running in the background (start_misses), if it is."""
    retries = [r for r in (retries or []) if available(j, r["action"])][:CARDS]
    misses = miss_scores(j, ahead.result() if ahead is not None else locate_misses(
        backend, j["activity"], principles_text(j["tree"]), ask_miss(backend, miss_prompt(j))))
    ranked = score_principles(j, misses)
    short = shortlist(j, ranked, exclude=[r["action"] for r in retries])
    n_new = max(CARDS - len(retries), MIN_CARDS - len(retries), 0)
    moments = {r["action"]: last_scene(j, r["action"]) for r in retries}
    picked = choose(backend, j, short, n_new, taken=[m for m in moments.values() if m])
    moments.update({aid: moment for aid, _, _, moment in picked})
    aids = [r["action"] for r in retries] + [p[0] for p in picked]
    cards = expand(backend, j, aids, moments)
    number = len(j["rounds"]) + 1
    rnd = {"number": number, "opened": now(), "closed": None, "lambda": round(miss_weight(j), 3),
           "misses": j.get("last_misses", []), "scores": ranked, "general": "", "summary": None, "cards": []}
    for r in retries:
        rnd["cards"].append({"action": r["action"], "principle": r["principle"], "why_chosen": "retry",
                             "moment": moments[r["action"]],
                             "retry": True, "reply": r["reply"], "card": cards[r["action"]],
                             "free": "", "guided_q": [], "guided_a": [], "outcome": None, "note": ""})
    for aid, pid, why, moment in picked:
        rnd["cards"].append({"action": aid, "principle": pid, "why_chosen": why, "moment": moment,
                             "retry": False, "reply": "",
                             "card": cards[aid], "free": "", "guided_q": [], "guided_a": [], "outcome": None,
                             "note": ""})
    for c in rnd["cards"]:
        j["tree"]["actions"][c["action"]]["suggested"].append(number)
    j["rounds"].append(rnd)
    return rnd


def current_round(j: dict) -> dict | None:
    return j["rounds"][-1] if j.get("rounds") and not j["rounds"][-1].get("closed") else None


# =============================================================================
# STEP 6: feedback (free, then guided, then general)
# =============================================================================
def ask_guided(backend, j: dict) -> None:
    """After the free feedback: 1-2 guided questions per card, aimed at its principle without naming it."""
    rnd, tree = current_round(j), j["tree"]
    items = []
    for c in rnd["cards"]:
        a = tree["actions"][c["action"]]
        items.append(f"[{c['action']}] {a['name']} [{tree['principles'][c['principle']]['name']}]\n"
                     f"  What stood out to them: {c['free'] or '(nothing written)'}")
    prompt = fill(PROMPT_GUIDED, activity=j["activity"], items="\n".join(items))
    got = {}
    try:
        for q in ask_json(backend, j, prompt + PROMPT_GUIDED_FORMAT, "guided").get("questions") or []:
            if isinstance(q, dict) and q.get("id"):
                got[q["id"]] = as_list(q.get("questions"))[:2]
    except RuntimeError:
        pass
    for c in rnd["cards"]:
        c["guided_q"] = got.get(c["action"]) or ["What was happening around you right when you tried it?"]
        c["guided_a"] = [""] * len(c["guided_q"])


# =============================================================================
# STEPS 7-8: close the round: assess, update, handle failures, plan the next one
# =============================================================================
def card_report(c: dict, tree: dict) -> str:
    a = tree["actions"][c["action"]]
    qa = "\n".join(f"  Q: {q}\n  A: {ans or '(no answer)'}" for q, ans in zip(c["guided_q"], c["guided_a"]))
    return (f"[{c['action']}] They were asked to: {a['name']} ({a['what']})\n"
            f"  What stood out to them: {c['free'] or '(nothing written)'}\n{qa}")


def close_round(backend, j: dict) -> dict:
    rnd, tree = current_round(j), j["tree"]
    ahead = start_misses(backend, j)    # for the next round, alongside the assessment and the update
    items = "\n\n".join(card_report(c, tree) for c in rnd["cards"])
    if rnd["general"]:
        items += f"\n\nIn general, apart from these, they wrote: {rnd['general']}"
    prompt = fill(PROMPT_ASSESS, activity=j["activity"], profile=profile_text(j), items=items)
    assessed = {}
    for x in ask_json(backend, j, prompt + PROMPT_ASSESS_FORMAT, "assess").get("assessed") or []:
        if isinstance(x, dict) and x.get("id"):
            assessed[x["id"]] = x
    retries, backtracks = [], []
    for c in rnd["cards"]:
        x = assessed.get(c["action"], {})
        c["outcome"] = x.get("outcome") if x.get("outcome") in OUTCOMES else "other"
        c["note"] = str(x.get("note") or "")
        reply = str(x.get("reply") or "")
        a = tree["actions"][c["action"]]
        if c["outcome"] == "engaged":
            a["state"] = "done"
            continue
        if c["outcome"] in FAILURES:
            a["attempts"] += 1
            a.setdefault("history", []).append({"round": rnd["number"], "outcome": c["outcome"],
                                                "free": c["free"], "guided": list(zip(c["guided_q"], c["guided_a"]))})
        if a["attempts"] >= MAX_ATTEMPTS:
            backtracks.append(c["action"])
        else:
            retries.append({"action": c["action"], "principle": c["principle"], "reply": reply})
    # everything they wrote updates the profile and the tree (including things unrelated to the cards)
    summary = update(backend, j, items, f"round {rnd['number']}")
    for aid in backtracks:
        summary.setdefault("backtracked", []).append(backtrack(backend, j, aid))
    for c in rnd["cards"]:   # an update may have crossed off a failed action (they did it after all)
        if tree["actions"][c["action"]]["state"] == "done":
            retries = [r for r in retries if r["action"] != c["action"]]
    j["boost"] = [pid for pid in j["boost"] if coverage(j, pid) < 1.0]
    rnd["summary"], rnd["closed"] = summary, now()
    plan_round(backend, j, retries, ahead)
    save(j)
    return summary


def backtrack(backend, j: dict, aid: str) -> dict:
    """After MAX_ATTEMPTS failures: name what's in the way as a principle, add easier actions toward the
    action, and put the action behind them. Its principle is boosted to the top until those are done."""
    tree, a = j["tree"], j["tree"]["actions"][aid]
    history = "\n".join(f"- (round {h['round']}, {h['outcome']}) {h['free']}" for h in a.get("history", []))
    prompt = fill(PROMPT_BACKTRACK, activity=j["activity"], k=a["attempts"], action=f"{a['name']} ({a['what']})",
                  history=history, profile=profile_text(j), principles="\n".join(
                      f"{pid} ({p['value']}, {tree['values'][p['value']]['name']}): {p['name']}"
                      for pid, p in tree["principles"].items()))
    data = ask_json(backend, j, prompt + PROMPT_BACKTRACK_FORMAT, "backtrack")
    p = data.get("principle") if isinstance(data.get("principle"), dict) else {}
    pid = p.get("id") if p.get("id") in tree["principles"] else None
    if pid is None:
        vid = p.get("value") if p.get("value") in tree["values"] else parents_value(tree, aid)
        pid = next_id(tree["principles"], "p")
        tree["principles"][pid] = {"name": str(p.get("name") or "Getting comfortable").strip(),
                                   "what": str(p.get("what") or "").strip(), "value": vid, "actions": [],
                                   "source": f"backtrack from {aid}"}
        tree["values"][vid]["principles"].append(pid)
    new_ids = []
    for raw in data.get("actions") or []:
        if isinstance(raw, dict):
            nid = new_action(tree, raw, f"backtrack from {aid}")
            if nid and nid != aid:
                attach(tree, nid, pid)
                new_ids.append(nid)
    a["blocked_by"] = new_ids
    a["attempts"] = 0
    if pid not in j["boost"]:
        j["boost"].append(pid)
    return {"action": aid, "principle": pid, "new_actions": new_ids, "why": str(data.get("why") or "")}


def parents_value(tree: dict, aid: str) -> str:
    ps = parents(tree, aid)
    return tree["principles"][ps[0]]["value"] if ps else next(iter(tree["values"]))


# =============================================================================
# PLAIN-WORDS SUMMARY OF A CLOSED ROUND (shown at the top of the next one)
# =============================================================================
def summary_text(j: dict, summary: dict | None) -> list[str]:
    if not summary:
        return []
    tree, lines = j["tree"], []
    names = lambda ids: ", ".join(tree["actions"][a]["name"].rstrip(".") for a in ids if a in tree["actions"])
    if summary.get("done"):
        lines.append(f"Already part of how you do it now, so it won't come up again: {names(summary['done'])}.")
    if summary.get("added"):
        lines.append(f"New things you mentioned, now noted: {names(summary['added'])}.")
    if summary.get("level") is not None:
        lines.append("Your experience level was updated from what you wrote.")
    for b in summary.get("backtracked", []):
        lines.append(f"\"{tree['actions'][b['action']]['name'].rstrip('.')}\" is on hold for now; "
                     f"you'll get some easier steps toward it first.")
    return lines



# =============================================================================
# TEMPORARY: PILOT MODE (11/13/26 test session). Delete this section once the test is over.
# =============================================================================
def review_round(backend, j: dict) -> dict | None:
    """PILOT: the tester has judged this round's cards (c["review"], rnd["review"], saved by the page).
    Move on as if they had done every card, so they see what would come next. The model is not shown
    what they wrote, and there is no assessment or profile update: nobody actually did the activity."""
    rnd, tree = current_round(j), j["tree"]
    ahead = start_misses(backend, j)
    for c in rnd["cards"]:
        c["outcome"] = "reviewed"
        tree["actions"][c["action"]]["state"] = "done"
    rnd["summary"], rnd["closed"] = {"pilot": True}, now()
    nxt = plan_round(backend, j, ahead=ahead)
    save(j)
    return nxt


def pilot_rows() -> list[dict]:
    """PILOT: every suggestion shown in a pilot journal, with what the tester said about it. One row per card."""
    rows = []
    for j in export_all():
        if j.get("mode") != "pilot" or not j.get("tree"):
            continue
        tree = j["tree"]
        for rnd in j.get("rounds", []):
            general = rnd.get("review") or {}
            for n, c in enumerate(rnd["cards"], 1):
                a, p, rv = tree["actions"][c["action"]], tree["principles"][c["principle"]], c.get("review") or {}
                rows.append({
                    "journal": j["slug"], "activity": j["activity"], "started": j["created"],
                    "first_answer": j["answer"], "level": j["profile"]["level"],
                    "round": rnd["number"], "card": n, "action": a["name"], "called": a.get("called", ""),
                    "card_title": c["card"].get("title", ""), "value": tree["values"][p["value"]]["name"],
                    "principle": p["name"], "impact": a.get("impact", ""), "overall_difficulty": a["overall"],
                    "principle_difficulty": a["principle"], "why_chosen": c.get("why_chosen", ""),
                    "rating": rv.get("rating", ""), "helps": rv.get("helps", ""), "instead": rv.get("instead", ""),
                    "round_deeper": general.get("deeper", ""), "round_other": general.get("other", ""),
                    "finished": j.get("finished", "")})
    return rows
