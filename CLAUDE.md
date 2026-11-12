# Dialectical Search v6: the value tree

v6 is the 10/28 overhaul in `SPEC_v6.md` (Travis's own write-up, kept verbatim, with a dated list of later changes at the end; read it first). v5 is
untouched in `../v5-dialectical-search/`. v6 is a fresh, much smaller backend. It doesn't extend v5's code,
because the spec replaces v5's model: the flat 8-12-value polygon becomes a tree, and the per-value moment
becomes a round chosen by gaps in a user profile. What v6 kept from v5:
- the backends (Ollama / Claude / mock; v6 adds the Claude API, now the default);
- the JSON parsing;
- the rule that the app knows no activity (enforced by a test);
- the card write-up style (scene / normally / steps / why), trimmed.

## Files

| File | What it is |
|---|---|
| `value_tree.py` | Everything: prompts, backends, the tree, the profile, scoring, rounds, feedback, backtracking, journals. |
| `app.py` | The Streamlit page. Draws `value_tree.py`; no decisions of its own. `streamlit run app.py` |
| `mock_backend.py` | A stand-in model for the tests and for clicking through the page without a model. |
| `test_value_tree.py` | `pytest -q`. Covers the tree, shared actions, scoring, retries, the backtrack, the profile, the tree built ahead, and that no prompt names an activity. The tests use the mock model, take under a second, and never run in the live app, so they have no effect on its speed. |
| `SPEC_v6.md` | The spec. |
| `journals/<slug>/` | `journal.json` holds everything, including every prompt and raw reply under `calls`. `tree.md` is a readable dump of the tree and profile. |

## The flow (the spec's steps, and where each one lives)

1. **Search box** with Travis's wording (`SEARCH_PROMPT`).
2. **Intake** (`intake`, `PROMPT_INTAKE`). Extracts the ACTIVITY, the knowledge lines, "not enough", an overall level from 1 to 10, and misc details. It also asks up to `MAX_QUESTIONS` short follow-up questions, once and only if needed.
3. **The tree** (`fetch_tree`, `build`, `PROMPT_TREE`). It goes root → 3-5 dialectical values → 2-4 principles each → 2-4 actions each. When there are follow-up questions, the tree call starts in the background as soon as the intake names the activity (see Speed).
   - An action can sit under several principles, even under different values. It is stored once (`tree["actions"]`), and every parent principle lists its id. Crossing it off therefore crosses it off everywhere, as the spec wants.
   - Each action has `overall` and `principle` difficulty ratings from 1 to 10.
4. **The profile**, in two places:
   - **In the tree:** each action's `state` is open, partial (downweighted) or done (crossed off).
   - **Outside the tree:** `level`, `principle_levels`, `knowledge`, `not_enough` and `misc`.

   `update()` (`PROMPT_UPDATE`) does every profile update. It runs after the intake and after every round. It crosses off actions, downweights others, and adds new actions for things the person mentions that the tree doesn't have, even things unrelated to the cards (the defender's-legs case). It also moves the level and adds misc details.
5. **Underweighted principles** (`score_principles`).
   - Each principle's gap is `1 − (done + 0.5·partial) / its number of actions`.
   - The gap is blended with the separate query "what do people at this level usually miss?" (`PROMPT_MISS`, `ask_miss`), whose answers `PROMPT_LOCATE` (`locate_misses`) places on the tree. The question is asked without the tree in view, so its answers can't lean on the tree's wording. Both calls run in the background alongside the profile update (`start_miss`, `start_misses`), so the question reads the level and knowledge lines as they were before that update.
   - The weight on that query is `MISS_WEIGHT / (1 + rounds closed)`, so 0.6, then 0.3, then 0.2, and so on: heavy at the start and lighter as the profile fills in.
6. **Choosing 2-3 actions** (`shortlist`, `choose`, `expand`).
   - The shortlist holds open actions that are within reach: `overall ≤ level + AWARENESS + 2` and `principle ≤ that principle's level + AWARENESS + 2` (AWARENESS = 1: the person is assumed to know a bit more than their rating, so the cards skip the basics). The tree prompt is told to be selective and leave out the obvious basics, not to cover every way of acting. Each action has a 1-10 `impact` rating (how much it changes how the activity goes for the person and whoever they do it with; the tree prompt asks for ratings spread over the range). Candidates come from every principle and are ranked by `need + IMPACT_WEIGHT × (impact − 5)/5 − STRETCH_WEIGHT × stretch − PARTIAL_PENALTY if partial`, where need is the principle's score, so a high-impact action can outrank a low-impact one from a slightly more neglected principle. The top 8 go to the choose prompt, which also prefers high impact.
   - Knowing a lot about one principle therefore unlocks harder actions in it, as the spec's example describes.
   - Actions about one step above the person come first.
   - The model picks from the shortlist using the misc details, and gives each pick a one-line `moment` for its card, different from each other and from the scenes of any retried cards in the round.
   - The cards are then written one call per card, all at the same time. Each call is given the whole list of this round's actions with their moments and writes one of them. A retried action keeps the scene its last card had (`last_scene`).
7. **Feedback.** It comes in three parts:
   - free feedback per card (`FREE_QUESTION`);
   - then 1-2 guided questions per card (`PROMPT_GUIDED`), aimed at the action's principle without naming it;
   - then general feedback (`GENERAL_QUESTION`).
8. **Close** (`close_round`, `PROMPT_ASSESS`). Each card is classified as engaged, misinterpreted, afraid, no_chance or other.
   - **Engaged:** the action is crossed off.
   - **Misinterpreted, afraid or other:** the action comes back next round with a reply. A misinterpretation is challenged and re-explained. A fear is affirmed, then the reasoning is explained.
   - **No_chance:** the action comes back, and the attempt doesn't count toward the limit.
   - **After 3 counted failures:** `backtrack()` (`PROMPT_BACKTRACK`) names what's in the way as a principle (an existing one or a new one). It adds 2-3 easier actions toward the stuck action, blocks the stuck action until those are done (`blocked_by`), and puts that principle at the top (`j["boost"]`) until it's covered.
   - Everything written (cards plus general) then goes through `update()`, and the next round is planned.

## Privacy (for running it online)

- Participants see no sidebar. Each journal has a random `key`; after the first answer the URL gets `?j=<key>`, and bookmarking it is how a participant comes back. `find_by_key()` is the only way a participant reaches a journal (slugs are guessable, keys aren't).
- The sidebar (backend choice, every journal, researcher view) appears only on `?researcher=1` after entering `RESEARCHER_PASSWORD` (env var or Streamlit secret). Unset means it can't be unlocked, so a deploy fails closed. Locally, put the password in `.env`.
- Participants use the backend named in `DTR_BACKEND`, default `api`: `ClaudeAPIBackend`, Sonnet 5.5 (`API_MODEL`) at effort `API_EFFORT` (medium), paid from API credits via `ANTHROPIC_API_KEY`, with server-side refusal fallback on. Ollama (`local`) and the personal-plan `claude` backend are only used when picked in the researcher sidebar.
- **Storage.** With `DATABASE_URL` set, journals live in one Postgres table `dtr_journals` (slug, key, activity, updated, data jsonb), created on first use; slugs get a random suffix so concurrent users never collide. Unset, they're files in `JOURNALS_DIR` as before. A journal holds every prompt and raw reply (`calls`), so it is the full log. The researcher sidebar has "Download all journals (JSON)".
- Testing the store: `TEST_DATABASE_URL=postgresql:///dtr_test pytest -q` (local `createdb dtr_test`); without it that test is skipped.

## Speed

The wait is all model calls. On real Sonnet 5.5 sessions the wait after the follow-up questions was 65-105 s (second intake ~5 s, tree 25-70 s, then update, miss, locate, choose and expand at ~4, 5, 3, 4 and 14 s), and closing a round took ~35-40 s. It is now whatever is left of the tree plus ~15 s, and ~15 s to close a round (real run, 2026-11-12: 17 s to the first cards for someone who took 70 s to answer, 15 s to close round 1). The order of calls:

```
first answer      intake
  questions shown   tree ........................... (background, while they type)
answers           intake
                  update (intake)  |  miss, then locate (background; miss starts before the tree is in)
                  choose
                  expand a | expand b | expand c
close a round     assess, then update (round n)  |  miss, then locate (background, started first)
                  [backtrack]
                  choose
                  expand a | expand b | expand c
```

Changes that leave every prompt as it was (2026-11-11):

- **The tree is built ahead** (`prefetch_tree`, `PREFETCH_TREE`). The tree prompt needs only the activity's name, so `start()` hands it to a background thread when there are questions, and `build()` picks the result up (`_trees`, keyed by the journal's key). If the early call failed or the server restarted in between, `build()` asks for the tree itself. Someone who never answers the questions still costs one tree call (about 6 cents on Sonnet 5.5); `PREFETCH_TREE = False` stops that.
- **The database connection stays open** (`_query`). Connecting took ~0.2 s and a statement ~0.03 s, and the page loads the journal on every click. A dropped connection is replaced once and the statement run again.
- **The model client is shared** (`make_backend` in `app.py`, `st.cache_resource`).

Changes that alter what the model is given (2026-11-12, Travis's choice):

- **The miss query runs alongside the update**, from the profile as it stood before the update. The knowledge lines the update adds from the newest feedback reach the miss query one round later. The miss and locate prompts themselves are unchanged.
- **One call per card, at the same time.** The cards can't see each other's text, so each call is told the moment every card is set in: the choose call writes a moment per pick (about a second more on that call), and a retried action keeps its last scene. A round is as slow as its slowest card call.

Tried and undone: **merging miss and locate into one call** (2026-11-12). With the principle list in view, the answers started reusing the list's wording, which defeats the point of a separate query. Running the two calls in the background costs nothing extra when a round closes and at most ~4 s on the first round, and only when the tree had already finished before the person continued.

Calls made in the background are logged when they are collected; `calls` is kept in time order (`log_calls`). Still one after another, because each needs the one before: assess → update, and choose → expand. Lower effort for the small steps is the remaining untried option.

## Choices the spec left open (ask Travis before changing)

- **The numbers.** All the numbers are in the SETTINGS block at the top of `value_tree.py`: 3-5 values, 2-4 principles, 2-4 actions, `STRETCH` = 2, `AWARENESS` = 1, `IMPACT_WEIGHT` = 0.5, `STRETCH_WEIGHT` = 0.05, `PARTIAL_PENALTY` = 0.25, `MISS_WEIGHT` = 0.6, partial = 0.5, 3 questions, 3 attempts.
- **Misc details.** They live outside the tree in `profile["misc"]`. The spec said it was unclear where they should go. They reach the choose, expand, assess and backtrack prompts through `profile_text()`.
- **Shared actions** have one pair of difficulty ratings. The "principle" check uses the person's best level among the action's parent principles.
- **"Won't be suggested again for a while"** is implemented as: done actions are never suggested again.
- **Level changes.** The overall level moves by at most 1 per update, so one enthusiastic reflection can't jump it.
- **The tree stays hidden** from the participant, as in v5. Each card has a "fits" line that says in plain words what bigger part of the activity it belongs to, without the hidden label. The spec asks that people see how each action fits, so this is the main place to revisit.
- **No chat.** The spec has none, so v5's chat isn't in v6. Every word the participant writes, and every prompt and reply, is saved in `journal.json`.

## Not done yet

- Real runs (2026-11-11, Travis's basketball answer): Sonnet 5.5 via the API gives named, well-spread actions and good first cards (space the floor, help side, shot-ready) in ~90 s for ~$0.13 per first round. Qwen3.5-9B runs but rates most high-impact actions as too hard for a beginner (they fall out of reach), invents some names, and the profile update can turn the person's own sentences into junk actions ("Casual Play").
- No eval like v5's `evals/` (for example, a goal-state check that a new player with Travis's basketball answer gets screens, boxing out and cuts first). That eval would carry over well.
- Researcher notes and journal export from v5 aren't ported.
