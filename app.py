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
"""

import hmac
import json
import os
import re

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
    answer = st.text_area(vt.SEARCH_PROMPT, height=160)
    if st.button("Search", type="primary") and answer.strip():
        with busy("Reading what you wrote"):
            j = vt.start(backend(), answer.strip())
        st.session_state.slug = j["slug"]
        if not researcher:
            st.query_params["j"] = j["key"]
        st.rerun()
    st.stop()

j = vt.load(slug)
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
    if go:
        with busy("Building your map of the activity"):
            vt.answer_questions(backend(), j, answers)
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
if prev and prev.get("summary"):
    lines = vt.summary_text(j, prev["summary"])
    if lines:
        st.success("Since last time: " + " ".join(lines))

st.subheader(f"Round {rnd['number']}: try these the next time you do it")
for c in rnd["cards"]:
    show_card(c)

st.divider()
stage = "guided" if rnd["cards"][0]["guided_q"] else "free"

if stage == "free":
    st.subheader("After you've done the activity")
    with st.form("free", border=False):
        texts = {}
        for c in rnd["cards"]:
            title = c["card"].get("title") or j["tree"]["actions"][c["action"]]["name"]
            texts[c["action"]] = st.text_area(f"**{title}.** {vt.FREE_QUESTION}", value=c["free"], height=120,
                                              key=f"free-{rnd['number']}-{c['action']}")
        col1, col2 = st.columns(2)
        save_only = col1.form_submit_button("Save for now")
        next_step = col2.form_submit_button("Next", type="primary")
    if save_only or next_step:
        for c in rnd["cards"]:
            c["free"] = texts[c["action"]].strip()
        if next_step:
            if not any(c["free"] for c in rnd["cards"]):
                st.warning("Write about at least one of them first.")
                vt.save(j)
                st.stop()
            with busy("Thinking of a few questions"):
                vt.ask_guided(backend(), j)
        vt.save(j)
        st.rerun()
else:
    st.subheader("A few more questions")
    with st.form("guided", border=False):
        for c in rnd["cards"]:
            title = c["card"].get("title") or j["tree"]["actions"][c["action"]]["name"]
            st.markdown(f"**{title}**")
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
        if close:
            with busy("Reading what you wrote and picking what's next"):
                vt.close_round(backend(), j)
        st.rerun()

# ---- 5. past rounds --------------------------------------------------------------------------
past = [r for r in j["rounds"] if r.get("closed")]
if past:
    with st.expander(f"Past rounds ({len(past)})"):
        for r in reversed(past):
            st.markdown(f"**Round {r['number']}**")
            for c in r["cards"]:
                title = c["card"].get("title") or j["tree"]["actions"][c["action"]]["name"]
                st.markdown(f"- *{title}*: {c['free'] or '(nothing written)'}")
            if r["general"]:
                st.markdown(f"- *Anything else:* {r['general']}")
