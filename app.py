"""
Dialectical Search v6: the website. Run:  streamlit run app.py   (http://localhost:8501)

Everything the app decides lives in value_tree.py; this file only draws it. The page re-runs top to
bottom on every click; the journal lives on disk (journals/<slug>/journal.json) and is reloaded each time,
so a round can stay open for days.

The page:
  1. no journal open: the search box (Travis's wording, value_tree.SEARCH_PROMPT)
  2. follow-up questions, if the model had any (asked once)
  3. a round: what changed since last time, then 2-3 cards (a retry shows the app's reply on top)
  4. after doing the activity: free feedback per card -> guided questions per card + general feedback
     -> "Close this round" (the next round opens)
  5. past rounds, folded
Privacy: participants see no sidebar and can only reach their own journal, through its private link
(?j=<key>, set after the first answer; bookmark it to come back). The sidebar (backend, every journal,
the researcher view of the hidden tree and profile) appears only on ?researcher=1 after entering
RESEARCHER_PASSWORD (env var or Streamlit secret). With no password set, it can't be unlocked.
Participants use the backend in DTR_BACKEND (default: api).

TEMPORARY: PILOT MODE (for a one-off test session on 11/13/26; see the note at the top of value_tree.py).
A journal started in pilot mode (j["mode"] == "pilot") shows a review form under the cards in place of
steps 4 above: testers judge each suggestion and say what they'd have wanted instead, then get the next
round as if they had done every card. The real flow below is unchanged and is what a "study" journal
gets. Everything pilot-only is marked PILOT.
"""

import csv
import hmac
import io
import json
import os
import re
import sys
import traceback

import streamlit as st

import value_tree as vt

st.set_page_config(page_title="Dialectical Search", layout="centered")

BACKENDS = {"Claude API (default)": "api", "Ollama (local)": "local", "Claude (my plan, just me)": "claude",
            "Mock (no model, for testing)": "mock"}


SECRETS_PROBLEM = ""   # why the Streamlit secrets couldn't be read, without any secret values


def setting(name: str, default: str = "") -> str:
    """An env var, or a Streamlit secret of the same name (for hosted deploys)."""
    global SECRETS_PROBLEM
    if os.getenv(name):
        return os.environ[name]
    try:
        value = st.secrets.get(name)
    except FileNotFoundError:   # no secrets at all (normal locally, where .env is used)
        return default
    except Exception as e:      # usually the Secrets box isn't valid TOML (e.g. KEY=value without quotes)
        where = re.search(r"line \d+(, column \d+)?", str(e))
        SECRETS_PROBLEM = f"{type(e).__name__}" + (f" at {where.group(0)}" if where else "")
        return default
    return default if value is None else str(value)


# hosted deploys keep keys in Streamlit secrets; value_tree and the SDKs read the environment
for _name in ("ANTHROPIC_API_KEY", "DATABASE_URL", "API_MODEL", "API_EFFORT"):
    if not os.getenv(_name) and setting(_name):
        os.environ[_name] = setting(_name)

RESEARCHER_PASSWORD = setting("RESEARCHER_PASSWORD")
vt.MODE = setting("DTR_MODE", vt.MODE)   # PILOT: "pilot" (the temporary default) or "study" (the real flow)
PUBLIC_BACKEND = setting("DTR_BACKEND", "api")
DEFAULT_MODELS = {"api": os.getenv("API_MODEL", "claude-sonnet-5-5"), "local": os.getenv("LOCAL_MODEL", "qwen3.5:9b"),
                  "claude": os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5"), "mock": "mock"}


@st.cache_resource(show_spinner=False)
def make_backend(kind: str, model: str):
    """One backend per (kind, model), shared by everyone, so its connection to the model stays open
    instead of being set up again on every click."""
    return vt.make_backend(kind, model)


def backend():
    kind = st.session_state.get("backend_kind", PUBLIC_BACKEND)
    return make_backend(kind, st.session_state.get("model") or DEFAULT_MODELS[kind])


def setup_problem() -> str:
    """What's missing for the participant backend, in words safe to show anyone (no secret values)."""
    if st.session_state.get("backend_kind", PUBLIC_BACKEND) != "api" or os.getenv("ANTHROPIC_API_KEY"):
        return ""
    if SECRETS_PROBLEM:
        return (f"The app's Secrets couldn't be read ({SECRETS_PROBLEM}). They must be TOML, with every value "
                f'in quotes: ANTHROPIC_API_KEY = "sk-ant-..."')
    return ('The app has no ANTHROPIC_API_KEY. In Manage app → Settings → Secrets, add it as TOML, with the value '
            'in quotes and at the top level (not under a [section]): ANTHROPIC_API_KEY = "sk-ant-..."')


def busy(label: str):
    return st.spinner(label + " (this can take a minute)")


def model_step(label: str, step, *args):
    """Run a step that calls the model. If it fails (the model service is busy, a dropped connection),
    say so plainly and return False, so the person can press the button again; nothing they wrote is lost."""
    try:
        with busy(label):
            step(*args)
        return True
    except Exception:
        traceback.print_exc(file=sys.stderr)
        st.error("That didn't go through: the model was busy or took too long. Nothing you wrote was lost. "
                 "Wait a few seconds and press the button again.")
        return False


# ---- who is this: a participant (default) or the researcher --------------------------------
if st.session_state.get("slug") is None and st.query_params.get("j"):
    st.session_state.slug = vt.find_by_key(st.query_params["j"])   # their private link
    if st.session_state.slug is None:
        st.query_params.clear()

researcher = st.session_state.get("researcher", False)
if not researcher and st.query_params.get("researcher") == "1":
    with st.sidebar:
        if not RESEARCHER_PASSWORD:
            st.caption("Set RESEARCHER_PASSWORD to unlock the researcher view.")
        else:
            pw = st.text_input("Researcher password", type="password")
            if pw and hmac.compare_digest(pw, RESEARCHER_PASSWORD):
                st.session_state.researcher = True
                st.rerun()
            elif pw:
                st.error("Wrong password.")

# ---- sidebar (researcher only) ---------------------------------------------------------------
if researcher:
    with st.sidebar:
        st.header("Setup")
        names = list(BACKENDS)
        default = next((i for i, n in enumerate(names) if BACKENDS[n] == PUBLIC_BACKEND), 0)
        choice = st.radio("Model", names, index=default)
        kind = BACKENDS[choice]
        if st.session_state.get("backend_kind") != kind:
            st.session_state.backend_kind, st.session_state.model = kind, DEFAULT_MODELS[kind]
        if kind != "mock":
            st.text_input("Model name", key="model")

        st.subheader("Journals")
        slugs = vt.list_journals()
        if slugs:
            cur = st.session_state.get("slug")
            pick = st.selectbox("Open", slugs, index=slugs.index(cur) if cur in slugs else 0)
            if st.button("Open journal"):
                st.session_state.slug = pick
                st.query_params.clear()
                st.query_params["researcher"] = "1"
                st.rerun()
        if st.button("New journal"):
            st.session_state.slug = None
            st.query_params.clear()
            st.query_params["researcher"] = "1"
            st.rerun()

        if st.session_state.get("slug"):
            j_side = vt.load(st.session_state.slug)
            with st.expander("Researcher view (hidden from participants)"):
                if j_side.get("tree"):
                    st.code(vt.tree_text(j_side["tree"]), language=None)
                st.markdown("**Profile**\n\n" + vt.profile_text(j_side).replace("\n", "\n\n"))
                if j_side.get("rounds"):
                    r = j_side["rounds"][-1]
                    st.markdown(f"**Round {r['number']}**: weight on the 'usually missed' query = {r['lambda']}")
                    st.markdown("Usually missed: " + "; ".join(r["misses"]))
                    st.markdown("Top principles: " + ", ".join(
                        f"{j_side['tree']['principles'][p]['name']} ({s})" for p, s in r["scores"][:4]))
                    for c in r["cards"]:
                        st.markdown(f"- {c['action']}: {c['why_chosen']}")
                if j_side.get("tree_problems"):
                    st.caption("Tree problems: " + "; ".join(j_side["tree_problems"]))
        st.subheader("Export")
        if st.button("Prepare all journals"):
            st.session_state.export = json.dumps(vt.export_all(), indent=2, ensure_ascii=False)
        if st.session_state.get("export"):
            st.download_button("Download all journals (JSON)", st.session_state.export,
                               file_name=f"dtr-journals-{vt.now()[:10]}.json", mime="application/json")
            st.caption("Everything: answers, cards, feedback, profiles, trees, and every prompt and model reply.")
        if st.button("Prepare tester feedback"):   # PILOT
            rows, out = vt.pilot_rows(), io.StringIO()
            if rows:
                w = csv.DictWriter(out, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
            st.session_state.pilot_csv = out.getvalue()
        if st.session_state.get("pilot_csv") is not None:
            st.download_button("Download tester feedback (CSV)", st.session_state.pilot_csv,
                               file_name=f"dtr-tester-feedback-{vt.now()[:10]}.csv", mime="text/csv")
            st.caption("One row per suggestion shown to a tester: what it was, where it sits in the tree, "
                       "their rating and what they wrote.")
        st.caption("Storage: " + ("database" if vt.database_url() else f"files in {vt.journals_dir()}/"))

        if st.button("Lock"):
            st.session_state.researcher = False
            st.query_params.clear()
            st.rerun()


# ---- 1. the search box -----------------------------------------------------------------------
st.title("Dialectical Search")
if setup_problem():
    st.error(setup_problem())
    st.stop()
slug = st.session_state.get("slug")

if not slug:
    if vt.MODE == "pilot":   # PILOT
        st.info(vt.PILOT_INTRO)
    answer = st.text_area(vt.SEARCH_PROMPT, height=160)
    if st.button("Search", type="primary") and answer.strip():
        started = {}
        if model_step("Reading what you wrote", lambda: started.update(j=vt.start(backend(), answer.strip()))):
            st.session_state.slug = started["j"]["slug"]
            if not researcher:
                st.query_params["j"] = started["j"]["key"]
            st.rerun()
    st.stop()

j = vt.load(slug)
pilot = j.get("mode") == "pilot"   # PILOT: this journal was started in the temporary test mode
st.caption(j["activity"])
if not researcher and j.get("key"):
    st.caption("Bookmark this page to come back to your journal later. Keep the link private: "
               "anyone with it can see what you wrote.")

# ---- 2. follow-up questions ------------------------------------------------------------------
if j.get("questions"):
    st.markdown("A couple of quick questions first:")
    with st.form("questions", border=False):
        answers = [st.text_input(q, key=f"q{i}") for i, q in enumerate(j["questions"])]
        go = st.form_submit_button("Continue", type="primary")
    if go and model_step("Building your map of the activity", vt.answer_questions, backend(), j, answers):
        st.rerun()
    st.stop()


# ---- 3. the round ----------------------------------------------------------------------------
def show_card(c: dict) -> None:
    card = c["card"]
    with st.container(border=True):
        if c.get("reply"):
            st.info(c["reply"])
        st.markdown(f"#### {card.get('title') or j['tree']['actions'][c['action']]['name']}")
        if card.get("scene"):
            st.markdown("\n\n".join(card["scene"]))
        if card.get("normally"):
            st.markdown(card["normally"])
        if card.get("steps"):
            st.markdown("\n".join(s if s[:2].rstrip(".").isdigit() else f"{i}. {s}"
                                  for i, s in enumerate(card["steps"], 1)))
        if card.get("called"):
            st.markdown(card["called"])
        if card.get("key"):
            st.markdown(f"**{card['key']}**")
        if card.get("why"):
            st.markdown("**Why it works.** " + " ".join(card["why"]))
        if card.get("fits"):
            st.markdown(card["fits"])
        if card.get("enough"):
            st.caption(card["enough"])
        if card.get("experiment") or card.get("watch"):
            with st.expander("Other times to try it"):
                st.markdown("\n".join(f"- {e}" for e in card.get("experiment", [])))
                if card.get("watch"):
                    st.markdown(f"*Watch for:* {card['watch']}")


rnd = vt.current_round(j)
prev = j["rounds"][-2] if len(j["rounds"]) > 1 else None
if prev and prev.get("summary") and not pilot:
    lines = vt.summary_text(j, prev["summary"])
    if lines:
        st.success("Since last time: " + " ".join(lines))


def card_title(c: dict) -> str:
    return c["card"].get("title") or j["tree"]["actions"][c["action"]]["name"]


def show_past_rounds() -> None:
    past = [r for r in j["rounds"] if r.get("closed")]
    if past:
        with st.expander(f"Past rounds ({len(past)})"):
            for r in reversed(past):
                st.markdown(f"**Round {r['number']}**")
                for c in r["cards"]:
                    if pilot:   # PILOT: what they said about the suggestion
                        rv = c.get("review") or {}
                        said = "; ".join(x for x in (f"{rv['rating']}/5" if rv.get("rating") else "",
                                                     rv.get("helps", ""), rv.get("instead", "")) if x)
                        st.markdown(f"- *{card_title(c)}*: {said or '(nothing written)'}")
                    else:
                        st.markdown(f"- *{card_title(c)}*: {c['free'] or '(nothing written)'}")
                if r["general"]:
                    st.markdown(f"- *Anything else:* {r['general']}")


if not rnd["cards"]:   # nothing left within reach (in the pilot, every card counts as done)
    st.info("There are no more suggestions that fit right now. Thank you for going through these.")
    show_past_rounds()
    st.stop()

if pilot and j.get("finished"):   # PILOT
    st.success("Thank you. Your feedback is saved, and you can close this page.")
    if st.button("Keep going: show me these suggestions again"):
        j.pop("finished")
        vt.save(j)
        st.rerun()
    show_past_rounds()
    st.stop()

st.subheader(f"Round {rnd['number']}: " + ("suggestions" if pilot else "try these the next time you do it"))
for c in rnd["cards"]:
    show_card(c)

st.divider()

if pilot:
    # ---- PILOT (temporary): judge the suggestions instead of trying them ---------------------
    activity = j["activity"]
    st.subheader("What do you think of these suggestions?")
    st.caption("You don't need to try them. Read them as someone fairly new to this activity would, and say "
               "how good they are as suggestions. Short answers are fine, and you can skip any question.")
    with st.form(f"review-{rnd['number']}", border=False):
        answers = {}
        for c in rnd["cards"]:
            rv, key = c.get("review") or {}, f"{rnd['number']}-{c['action']}"
            st.markdown(f"#### {card_title(c)}")
            rating = st.radio(vt.fill(vt.PILOT_RATING, activity=activity), [1, 2, 3, 4, 5], horizontal=True,
                              index=rv["rating"] - 1 if rv.get("rating") else None, key=f"rate-{key}")
            st.caption(vt.PILOT_SCALE)
            helps = st.text_area(vt.fill(vt.PILOT_HELPS, activity=activity), value=rv.get("helps", ""), height=90,
                                 key=f"helps-{key}")
            instead = st.text_area(vt.PILOT_INSTEAD, value=rv.get("instead", ""), height=90, key=f"instead-{key}")
            answers[c["action"]] = {"rating": rating, "helps": helps.strip(), "instead": instead.strip()}
        st.markdown("#### Overall")
        general = rnd.get("review") or {}
        deeper = st.text_area(vt.fill(vt.PILOT_DEEPER, activity=activity), value=general.get("deeper", ""),
                              height=110, key=f"deeper-{rnd['number']}")
        other = st.text_area(vt.PILOT_OTHER, value=general.get("other", ""), height=90, key=f"other-{rnd['number']}")
        col1, col2 = st.columns(2)
        finish = col1.form_submit_button("Save and finish")
        more = col2.form_submit_button("Save and see the next suggestions", type="primary")
    if finish or more:
        for c in rnd["cards"]:
            c["review"] = {**answers[c["action"]], "time": vt.now()}
        rnd["review"] = {"deeper": deeper.strip(), "other": other.strip(), "time": vt.now()}
        if finish:
            j["finished"] = vt.now()
        vt.save(j)   # what they wrote is stored before the model is asked for anything
        if finish or model_step("Picking the next suggestions", vt.review_round, backend(), j):
            st.rerun()
    show_past_rounds()
    st.stop()

# ---- 4. the real flow: feedback after doing the activity ---------------------------------------
stage = "guided" if rnd["cards"][0]["guided_q"] else "free"

if stage == "free":
    st.subheader("After you've done the activity")
    with st.form("free", border=False):
        texts = {}
        for c in rnd["cards"]:
            texts[c["action"]] = st.text_area(f"**{card_title(c)}.** {vt.FREE_QUESTION}", value=c["free"], height=120,
                                              key=f"free-{rnd['number']}-{c['action']}")
        col1, col2 = st.columns(2)
        save_only = col1.form_submit_button("Save for now")
        next_step = col2.form_submit_button("Next", type="primary")
    if save_only or next_step:
        for c in rnd["cards"]:
            c["free"] = texts[c["action"]].strip()
        vt.save(j)
        if next_step:
            if not any(c["free"] for c in rnd["cards"]):
                st.warning("Write about at least one of them first.")
                st.stop()
            if model_step("Thinking of a few questions", vt.ask_guided, backend(), j):
                vt.save(j)
                st.rerun()
        else:
            st.rerun()
else:
    st.subheader("A few more questions")
    with st.form("guided", border=False):
        for c in rnd["cards"]:
            st.markdown(f"**{card_title(c)}**")
            st.caption(f"You wrote: {c['free'] or '(nothing)'}")
            for i, q in enumerate(c["guided_q"]):
                c["guided_a"][i] = st.text_area(q, value=c["guided_a"][i], height=90,
                                                key=f"g-{rnd['number']}-{c['action']}-{i}")
        general = st.text_area(vt.GENERAL_QUESTION, value=rnd["general"], height=110, key=f"gen-{rnd['number']}")
        col1, col2 = st.columns(2)
        save_only = col1.form_submit_button("Save for now")
        close = col2.form_submit_button("Close this round", type="primary")
    if save_only or close:
        rnd["general"] = general.strip()
        for c in rnd["cards"]:
            c["guided_a"] = [a.strip() for a in c["guided_a"]]
        vt.save(j)
        if not close or model_step("Reading what you wrote and picking what's next", vt.close_round, backend(), j):
            st.rerun()

# ---- 5. past rounds --------------------------------------------------------------------------
show_past_rounds()
