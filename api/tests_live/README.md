# `api/tests_live/` — the live agent-surface suite


Current surface contract: the Agent cell holds one generator, `agent`,
whose `surface` setting picks `annotation` (the default), `chat` or
`terminal` (the CLI). An Annotation run serves no file: it reads the
messages users post into its annotation context (the cell's status pane;
`/api/channels/{ch}/cells/{cell}/annotations`), pulls them read and answers
with replies in their threads. A one-shot (`session: oneshot`) answers its
saved prompt, which the host seeds as the context's first, already-read
message, and ends itself. Live coordinates come from a mounted file's
`/surfaces/<path>` metadata, which renderers read through `getSurface()`.
An interactive run ends by its Stop and nothing starts by itself: the next
run is one the user starts. A run's end seals its context, which its Logs
keep as `annotations.json`. Historical passes below predate this contract;
their old timing and protocol evidence is not a pass claim for it, and the
annotation scenarios have no live pass yet.

Everything here drives the REAL product path: a real uvicorn, a real
channel, a real headless Chromium, a real agent harness (the local `claude`
CLI, or a container, both billed to a key you already have). Nothing runs by
default, and nothing here holds a secret.

It is the suite agent-v2 §9.1.7 asks for — the matrix that was previously
re-verified by hand every round: a terminal run per harness, the model the
bound account names, a posted annotation read and answered in its thread,
and the attachment path. Since
2026-09-26 it also carries the multi-runner scenarios
(`test_live_multi_runner.py`): several runs on one cell, each in its own
run folder `_runs/<label>/`, on the provider-generic pydantic-ai gate.

## The scenarios

| Test | Contract |
| --- | --- |
| `..._reasoning_block_reads_as_prose` | the saved `thinking` reads as prose (no blank line mid-sentence), and the model that answered is the one the runner's bound account names — checked against claude's own session jsonl, not our transcript |
| `..._message_sent_midstream_queues` | §9.1.3 — a message sent while a turn streams queues behind it: both replies land and the first is not cut short |
| `..._annotation_run_reads_a_posted_message_and_replies_in_its_thread` | an interactive Annotation run listens to its context; a message posted there is pulled read and answered with a reply in its thread (the run's label as author), which the run's drilldown shows; the run writes no chat files; its Stop seals the context, kept in its Logs as `annotations.json` |
| `..._composer_attachment_lands_in_the_cell` | an attachment becomes `attachments/<name>` in the cell and the reply cites that path |
| `..._terminal_run_tails_its_transcript_and_stops[opencode]` / `[goose]` | a terminal run's turn is tailed into its terminal transcript DURING the TTY (the tail holds the record's last reply back until something follows it, so the scenario sends a second prompt and the tail's own event lines must count ≥ 3 merged messages, the first prompt, its reply and the second prompt; the leg's finalize adds the second reply; the transcript is a log-category write that lands in the run log at the run end, where an ASSISTANT message must say MERIDIAN and there must be at least as many messages as were tailed live); the run's Stop (in its drilldown) ends the TTY and the run, with the pointer gone and no harness context file in the cell; no run starts by itself |
| `test_live_host_terminal_runs.py` `[claude-code]` / `[antigravity]` | a CLI run whose harness CLI runs on this machine, on a Key manager entry with nothing pasted (configured; Verify says the CLI runs on this machine): the browser's terminal mounts and attaches, the PTY child is this machine's CLI labelled with the run (`COLLIMATE_HOST_TERMINAL`), in the entry's per-account home under the host cache and with no key in its environment; Stop ends its whole process group; a second run on the entry runs out of the same home. No sign-in happens (the user's), so nothing is billed; gated on the CLI being on this machine (the server inherits the test's PATH) |

Each scenario owns its cell, so a wedged session can never reach the next test.

**Pass of 2026-09-26 (the multi-runner rework)** — the whole tree on
OpenRouter `deepseek/deepseek-v4-flash` without the claude-code gate:
13 passed, 9 skipped (the six claude-code scenarios and the three
session shapes, as their gates say), 0 failed in 5m27s — the two container
legs, the six manager-UI tests and the five multi-runner scenarios in one
run (`tmp/multi-runner/WP-F3/run10-full-openrouter/pytest.log`). The five new
multi-runner scenarios also passed on Cerebras and Gemini (see *The
multi-runner scenarios*). The container legs (opencode 1.18.32, goose
1.52.0) ran through the new run-folder wire for the first time: the
terminal pointer at `_runs/<label>/terminal/<backend>.terminal` and the
log-category transcript. One finding:

* **An empty model answer is a model failure.** One opencode run's turn
  ended in 2.0s with no text (`Build · DeepSeek V4 Flash · 2.0s`, nothing
  under it), so the tail had only the prompt to merge; the leg failed on
  `tailed >= 2` as it should (`run2-opencode`). Not reproducible on the
  next run.

**Last full pass: 2026-09-25** — all 17 tests in this tree (the eight
agent-surface scenarios, the six manager-UI ones and the three session
shapes) green in 9m39s, in headless Chromium 153 (python-playwright 1.63.0):
the claude-code scenarios on this machine's Claude SUBSCRIPTION (the CLI's
own login, see *What selects what*) with `haiku`, the opencode (1.18.32) and
goose (1.52.0) scenarios on OpenRouter with `deepseek/deepseek-v4-flash`;
claude ran through claude-agent-acp 0.81.2. That was the first live run of
the one-surface-per-run scenarios. The same day all 17 passed again after
the server moved to the in-memory keyring and the terminal check was
tightened: the 8 container + manager-UI tests in one run (3m12s), the 9
claude-code-backed ones in a second (6m10s) once the CLI login's access token
had been refreshed — the first attempts skipped on the 30-minute expiry
guard, as designed. Two things the first pass settled:

* **The terminal transcript is a LOG-category write.** The tail appends to
  `_agent/terminal/transcript.json` with `gen.update_file(..., category="log")`,
  which reaches the host at the run's next flush — the run END. So mid-TTY
  the transcript is in neither the cell nor the run log; the terminal
  scenario observes the tail by its own event lines
  (`…transcript.json: N message(s) tailed live from the <backend> terminal`,
  one per appended batch) while the run is live, summing N until the first
  prompt, its reply and a second prompt are in (≥ 3: the tail holds the
  record's last reply back until something follows it, since a reply that
  has held still can still grow, so the second prompt is what settles the
  first reply live, and the leg's finalize adds the second reply), and
  asserts the transcript's content on the run log once the run has ended —
  an ASSISTANT message saying MERIDIAN, since the prompt itself names the
  word.
* **claude omits thinking text unless asked.** Since claude 2.1.x the API
  returns thinking blocks as a bare signature unless the CLI sets
  `showThinkingSummaries`; the chat leg now passes it in its flag settings
  (`acp_run.py`, next to `claude_command_settings`), without which the
  reasoning scenario finds an empty Reasoning block.

## How a scenario starts a run

A run is a saved RUNNER against one cell — `POST /api/channels/{ch}/runs`
uses the immutable UUID returned by channel creation for `{ch}`; labels are
display metadata. It takes `{runner, runner_revision, cell_id, new_cell}` and
nothing else. So
each scenario, in order:

1. creates an **account** through `/api/accounts`: a base API-key account for
   the provider, plus a **model entry** under it carrying the model id and
   `metadata.harness` (`claude-code`, `opencode`, `goose`). The agent's
   `backend` setting reads that harness off the bound entry, and the model,
   endpoint and protocol come from the same place — there is no per-run
   model or provider to pass;
2. posts a **runner** built from one of a generator's shipped configs with
   that entry bound to its `model` role. The Agent cell's `agent` takes its
   surface from the runner's `surface` setting: `annotation` (messages in
   its context, no file), `chat` or `terminal` (a TTY in its container,
   docker: full; Claude Code's, and Antigravity's on the Google plan, the
   vendor's CLI on this machine, docker: none or full with commands ask or
   allow). Its config ids are one per harness — `default`
   (pydantic-ai), `claude-code`, `opencode`, `goose`, … — plus the one-shots
   such as `summarize` (annotation surface, `session: oneshot`) and `guide`
   / `superpowers` (chat); `runners(..., surface=…)` puts a harness preset on
   another surface;
3. starts it with `api.start_runner(cell, runner)`. Nothing starts a run by
   itself: after a run ends, `api.active_run_on(cell, besides=<the ended
   run>)` stays None until the scenario starts the next runner.

Two consequences for anyone writing a scenario:

* **an interactive session seeds no opening message.** Wait for
  `api.wait_idle(run)` and give it work the way a user does: type in a
  chat's composer (`view.send(...)`), or post into an Annotation run's
  context (`post_annotation(api, cell, run, text)`, then
  `wait_reply(api, cell, run, message_id)`).
* **the model cannot be switched mid-run.** A session's config controls are
  its session MODES; model and harness changes mean editing the account or
  the runner, so a run answers on one model from start to finish. The
  reasoning scenario asserts exactly that against claude's own records.

Accounts are created once per session and deleted at the end; runners are
made by the `runners(config_id, model, name, generator=…)` fixture from a
generator's shipped config as it ships, and deleted when the test ends.

## Running it

Once per venv (the workspace venv at the repo root), python-playwright and
its Chromium (the browser builds live in `~/.cache/ms-playwright`):

```bash
uv pip install --python .venv/bin/python playwright
.venv/bin/python -m playwright install chromium
```

```bash
cd api
# the claude-code scenarios (the local `claude` CLI) on this machine's Claude
# subscription — no key needed when `claude` is logged in
COLLIMATE_LIVE_CLAUDE_CODE=1 ../.venv/bin/python -m pytest tests_live -q
# … or billed to an API key instead
COLLIMATE_LIVE_CLAUDE_CODE=1 ANTHROPIC_API_KEY=… ../.venv/bin/python -m pytest tests_live -q

# the containerized harnesses (opencode, goose) — needs an OpenRouter key
COLLIMATE_LIVE_DOCKER=1 COLLIMATE_LIVE_OPENROUTER_MODEL=deepseek/deepseek-v4-flash \
    ../.venv/bin/python -m pytest tests_live -q -k container

# the multi-runner scenarios on the in-process pydantic-ai harness (no
# container) — one provider at a time: openrouter, cerebras or gemini
COLLIMATE_LIVE_PROVIDER=cerebras CEREBRAS_API_KEY=… COLLIMATE_LIVE_MODEL_CEREBRAS=qwen-3.8-27b \
    ../.venv/bin/python -m pytest tests_live/test_live_multi_runner.py -q

# everything (20 tests)
COLLIMATE_LIVE_CLAUDE_CODE=1 COLLIMATE_LIVE_DOCKER=1 \
    COLLIMATE_LIVE_OPENROUTER_MODEL=deepseek/deepseek-v4-flash \
    COLLIMATE_LIVE_PROVIDER=openrouter COLLIMATE_LIVE_MODEL_OPENROUTER=deepseek/deepseek-v4-flash \
    ../.venv/bin/python -m pytest tests_live -q
```

**Use `../.venv/bin/python -m pytest`, not `uv run pytest`.** Playwright is
deliberately NOT a dependency in `api/pyproject.toml` (CI never runs this
tree, and a browser driver has no place in the app's install), so it is
installed into the venv by hand as above — and a `uv run`/`uv sync` may
re-sync the environment and remove it (the suite then skips with
"python-playwright is not installed"). If you want `uv`, ask for it
explicitly: `uv run --with playwright pytest tests_live`.

`pyproject.toml` sets `testpaths = ["tests"]`, so a bare `pytest` — and CI —
never collects this tree; the exclusion is structural, not a marker anyone
has to remember. It also sets `python_files = ["test_pytest_*.py"]`
(`tests/test_*.py` are standalone scripts), which is why this
directory's `conftest.py` carries a `pytest_collect_file` hook opting
`test_live_*.py` in (and standing down for a file named on the command line,
which pytest collects by itself — otherwise every test would run twice). `uv run pytest tests` collects 1048 tests and none of
them are here.

## What selects what

| Variable | Effect |
| --- | --- |
| `COLLIMATE_LIVE_CLAUDE_CODE=1` | the claude-code scenarios, and the session shapes when no `ANTHROPIC_API_KEY` is set. Needs a `claude` on PATH and one of the credentials below |
| `ANTHROPIC_API_KEY` | when set, the claude-code account is an Anthropic **API-key** account (and the session shapes run on pydantic-ai with it) |
| (no key) the Claude subscription | otherwise the gate makes a **subscription** account, the shape the Key manager's Anthropic → Subscription form saves (`auth_type: subscription`, `secrets.access_token`, `metadata: {harness: claude-code, plan: claude_code}`), verified through `/api/accounts/check` first. The token is `CLAUDE_CODE_OAUTH_TOKEN` (what `claude setup-token` prints) when set, else the ACCESS token of the CLI's own login, read by path from `$CLAUDE_CONFIG_DIR/.credentials.json` (default `~/.claude/.credentials.json`). Only the access token is taken — never the refresh token, which the CLI rotates — and it lives only in the test process and the test server's account (deleted at the end); a skip names the path, never the value. A token that expires within 30 minutes skips, saying to run any `claude` command to refresh it. The CLI login's access token lives for hours, where the `claude setup-token` token a user pastes into the Key manager lasts a year: the fallback is a test convenience that exercises the same account shape, Verify and `CLAUDE_CODE_OAUTH_TOKEN` hand-off, not the setup-token itself — export `CLAUDE_CODE_OAUTH_TOKEN` for a run that must use one |
| `COLLIMATE_LIVE_DOCKER=1` | the opencode/goose scenarios (4–5). May build images on first run |
| `OPENROUTER_API_KEY` | required by the docker scenarios — read from the environment **or** the OS keychain (`collimate` service), because that is where a developer's key actually lives. The value is read here and sent to the test server as the account's secret |
| `COLLIMATE_LIVE_CLAUDE_MODEL` | the model of the claude-code account entry (default `haiku`) |
| `COLLIMATE_LIVE_OPENROUTER_MODEL` | the model of the opencode/goose account entries (default `openai/gpt-5-mini`; the 2026-09-25 pass used `deepseek/deepseek-v4-flash`, the cheap fixed model of the live rounds) |
| `COLLIMATE_LIVE_PROVIDER=openrouter\|cerebras\|gemini` | the multi-runner scenarios (`test_live_multi_runner.py`): an API-key account for that service (`credential_account(service=provider)`) with a model entry on the in-process **pydantic-ai** harness — no container. Needs the provider's standard key (`OPENROUTER_API_KEY` — env or keychain —, `CEREBRAS_API_KEY`, `GEMINI_API_KEY`) and the model below; each missing piece skips, named |
| `COLLIMATE_LIVE_MODEL_<PROVIDER>` | the model of that entry (`COLLIMATE_LIVE_MODEL_CEREBRAS=qwen-3.8-27b`, `COLLIMATE_LIVE_MODEL_GEMINI=gemini-flash-latest` — the bare Gemini API id; for openrouter `COLLIMATE_LIVE_OPENROUTER_MODEL` is accepted too) |

A missing precondition **skips with the reason named** — never passes
vacuously. A machine with no opt-in reports "14 skipped" (the six
agent-surface scenarios, the three session shapes and the five multi-runner
scenarios), each line saying what to set; the six manager-UI tests have no
gate and run.

## What the assertions are written against

Every assertion states the §9.1 contract as it stands: the live terminal
tail, queue-not-redirect, the run end clearing the live interaction files,
a posted message read and answered in its thread, `_runs/<label>/terminal/<backend>.terminal`. What
a seeded channel runs is the VENDORED generator (see *Known limitations*), so
a red run means that copy stopped keeping one of them.

## The offline renderers — `test_live_offline_renderers.py`

The module mirror's end-to-end check: the shipped renderers mount with both
module CDNs out of reach. The module starts its own server whose HTTPS proxy
is a closed port (so the mirror cannot fetch upstream) and a browser context
that aborts every `https://esm.sh/**` and `https://cdn.jsdelivr.net/**`
request. Per renderer (milkdown `.md`, mermaid `.mmd`, terminal and assistant
starting pointers, excalidraw, pdf, monaco `.py`) a channel of its own holds
one file; the test opens it and asserts the renderer mounted, the browser
asked no CDN, every `/api/mirror/` answer was 200/304, the server stored no
`esm_modules` row and logged no mirror failure. The markdown file also holds
a bullet list, which must render as list items in the editor (`.ProseMirror
ul li`): the milkdown renderer's `deps=` list pins prosemirror-model 1.25.11,
which the snapshot freezes (1.25.12 renders no list). It needs the built SPA and python-playwright only (no model,
account or Docker).

**First pass, 2026-10-25**: mermaid, terminal, excalidraw, pdf and monaco
passed; milkdown and the assistant failed on the snapshot's `unavailable`
URLs (`prosemirror-view@^1.x?deps=prosemirror-model@1.25.11` and
`react-markdown@10.1.0/X-…/react-markdown.mjs`, which esm.sh answered 500 at
freeze time and still did that day). They pass once the SDK freeze is re-run
with esm.sh serving them and the templates are re-vendored.

**Second pass, 2026-10-25, after `deps=` was removed** (React 19.3.1, the
snapshot re-frozen from plain builds): mermaid, terminal, excalidraw, pdf and
monaco passed; milkdown mounted but rendered no list items (`RangeError:
Expected value of type boolean for attribute spread on type list_item`: the
plain graph's prosemirror-model 1.25.12 against crepe 7.21.2), and the
assistant failed on the 41 `unavailable` `@assistant-ui/core@0.2.23` /
`store@0.2.x` builds esm.sh would not build. See
`docs/ai_tools/notes/2026-10-05/project_esm_transitive_pin_drift.md`.

**Third pass, 2026-10-25, with `deps=` restored** (React 19.2.0, the first
pass's snapshot: 1819 entries, six `unavailable`): mermaid, terminal,
excalidraw, pdf and monaco passed; milkdown failed only on the 502s for
`prosemirror-view@^1.0.0` and `@^1.41.3` `?deps=prosemirror-model@1.25.11&target=es2022`,
the assistant only on
`react-markdown@10.1.0/X-ZHJlYWN0QDE5LjIuMA/es2022/react-markdown.mjs`, all
in the snapshot's `unavailable` (esm.sh still answered 500 at the day's
freeze). The same as the first pass: the gap closes on the next `freeze`
that esm.sh serves, then a re-vendor.

**After the React bump, 2026-10-25** (React 19.3.1 in every renderer graph,
the snapshot complete: 1892 entries, 0 `unavailable`): all seven passed,
milkdown's list items and the chat's typing included.

## The manager UI suite — `test_live_manager_ui.py`

The same server and browser fixtures, one layer away from the agent: the Key
manager, the runner editor and the runners panel, driven through the shipped
SPA with no harness involved. It has **no provider gate** — it runs wherever
the SPA is built and python-playwright has a Chromium:

```bash
cd api
uv run --with playwright python -m pytest tests_live/test_live_manager_ui.py -q
# a Chromium for that playwright, if the launch is refused:
uv run --with playwright python -m playwright install chromium
# copies of the screens' screenshots, on top of the evidence dir:
COLLIMATE_LIVE_SHOTS=/some/dir … pytest tests_live/test_live_manager_ui.py -q
```

| Test | Contract |
| --- | --- |
| `test_key_manager_adds_a_custom_account_through_type_then_detail` | Add account → Custom → Next fixes the type before the detail; Name is first; a custom entry's credentials are its secret custom fields (no auth method); Verify reports on the open form without saving; Save returns to the list |
| `test_key_manager_supported_account_verify_explains_a_bad_key` | Supported service → alphabetical Service and Authentication lists → a fake OpenAI key fails Verify with the service's message and the catalog's help sentence → Save still stores it |
| `test_runners_panel_add_runner_edits_config_then_selects_the_saved_runner` | Add runner (a step with Back) → Generator → Next → Config / Advanced / Files / Security, each account role a key-icon row → Save returns with the runner selected and the Run / Edit / Duplicate / Delete mode tabs |
| `test_key_manager_plan_login_is_pasted_and_verified_offline` | a ChatGPT-plan account: the instruction under Authentication says what to paste, the App select names the login file, the pasted document is verified with no network call, and Save keeps it; no Sign in / Sign out / Pull login buttons |
| `test_runners_panel_tab_cycles_the_verb_and_enter_duplicates_then_deletes` | the first search match is selected in Run mode; Tab cycles Run → Edit → Duplicate → Delete and back to Run; Enter on Duplicate adds a "<name> copy" runner; a click selects a row; Enter on Delete opens the confirm strip above the footer, Enter again deletes |
| `test_runners_panel_run_posts_the_run_for_the_selected_runner` | Enter in Run mode posts `POST /api/channels/{ch}/runs` for that runner (intercepted and refused, so nothing runs) |

Two things this suite needed from the harness:

* **Its own accounts registry.** `accounts.py` keeps the registry at
  `~/.config/collimate/accounts.json` — the developer's own — so the server
  gets `COLLIMATE_ACCOUNTS_CONFIG=<data dir>/accounts.json`, and a keyring of
  its own: `_memory_keyring`, which stores for the life of the server process
  (the null backend refuses every account save with a 503).
* **A panel session is channel state.** Opening a cell's runners panel writes
  the session to the channel KV, so a panel a test leaves open re-mounts on
  every later page. `RunnersPanel` scopes each locator to
  `[data-pick-surface][data-pick-cell='<id>']` and closes the panel in the
  test's `finally`; a page-wide `[role='option'][aria-selected='true']` would
  match one row per open panel.

## The session shapes — `test_live_session_shapes.py`

The agent run shapes on a real model, made the product way (an Anthropic
account and a haiku model entry through the accounts API, runners from the
agent's shipped `summarize` config and its `default` config on the
annotation and chat surfaces, bound to that model). With
`ANTHROPIC_API_KEY` set, an API-key account and a haiku entry on the
in-process pydantic-ai harness; the key reaches only the test server, lives
in its own registry and is deleted with the account. Without it, and with
`COLLIMATE_LIVE_CLAUDE_CODE=1`, the shapes run on the claude-code entry the
`claude_code` gate makes from this machine's Claude subscription — the shapes
are the generator's, not the harness's.

| Test | Contract |
| --- | --- |
| `test_oneshot_answers_its_prompt_in_its_context_and_ends_itself` | the prompt is the context's first message (kind `prompt`, already read) before the run starts; the one-shot opens no file, never listens, replies in the prompt's thread and completes on its own; `summary.md` stays in the cell and the sealed context is in the run's Logs as `annotations.json` |
| `test_default_annotation_runner_opens_no_file_and_listens` | the agent's `default` preset is an interactive Annotation run: `/runs/active` marks it listening, it goes idle with an empty context and no chat files, and its row in the status pane carries no button |
| `test_default_chat_runner_opens_a_chat_with_no_seeded_turn` | the agent's `default` preset on the chat surface goes idle with an empty transcript (no opening message); the run's row in the status pane offers only to open its chat (`status-run-open-ui`) |

```bash
cd api
ANTHROPIC_API_KEY=… ../.venv/bin/python -m pytest tests_live/test_live_session_shapes.py -q
# or on the Claude subscription
COLLIMATE_LIVE_CLAUDE_CODE=1 ../.venv/bin/python -m pytest tests_live/test_live_session_shapes.py -q
```

## The multi-runner scenarios — `test_live_multi_runner.py`

Several runs on one cell, end to end on a real model. Every run owns a
folder `_runs/<label>/` in the cell (`label = <runner id>-<run id[:8]>`):
the host writes the run card `<label>.run` there before the generator
starts, the generator writes its UI files there, and the folder is swept
when the run ends. The head's run light counts the cell's live runs
(`data-run-count`); the head's status caret (`runs-pane-toggle`) opens the
cell's status pane (`data-runs-pane`), whose Status view (`status-view`)
has one row per live run (`status-run-row[data-run-id]`: its state, runner,
whether it listens and the elapsed time; at most an open-file button,
`status-run-open-ui`); a row opens the run's drilldown (`run-drilldown`),
whose header carries its Stop (`run-stop`). The Run shortcut (Alt+R) opens
the cell's runners panel whatever is focused. An Annotation run's folder
holds only its run card.

The agent runs are the agent generator's shipped `default` config
(pydantic-ai) on its annotation and chat surfaces; the web run is the shipped `web` config as it is. All
five run on the `provider_model` gate (`COLLIMATE_LIVE_PROVIDER`, above):
an API-key account for the provider plus a model entry on the in-process
pydantic-ai harness, no container.

| Test | Contract |
| --- | --- |
| `test_two_agent_runs_share_a_cell` | an annotation run and a chat run on one cell, both live: each has its own folder and run card, the chat run its pointer registered to it; `/runs/active` names both with distinct labels; the run light counts 2, the Status view lists both runs and the chat run's drilldown carries its Stop |
| `test_run_in_new_cell_clones_the_cell_beside_its_source` | with a run live, the runners panel opens (with its collision note); *in new cell* puts a clone (same name, the same files minus `_runs/`) in a new stack right of the source's and starts the runner there; the source and its run are untouched; the page attaches the clone's run |
| `test_stopping_one_run_from_the_status_pane_leaves_the_other_live` | Stop in the chat run's drilldown ends THAT run only: the annotation run and its folder stay, the chat run's folder is swept and its pointer and transcript move to its run Logs; the light counts 1 |
| `test_web_and_agent_runs_share_a_cell_and_the_tab_toolbar_captures_and_opens_tabs` | the shipped web runner beside a chat run; a run's row in the status pane carries at most its open-file button; the tab's own toolbar Capture reaches the web run as a press on that tab (its log names capture #1), and its New tab writes `tab-2.webview` and reveals it as the active tab. Headless Chromium has no native guest: in the webview's fallback view a capture has no guest to read and is refused, so no `<host>-<timestamp>/` folder lands here — the press reaching the run, the file and the reveal are the contract; a capture that writes its folder is an owner check in the desktop app |

```bash
cd api
COLLIMATE_LIVE_PROVIDER=openrouter OPENROUTER_API_KEY=… COLLIMATE_LIVE_MODEL_OPENROUTER=deepseek/deepseek-v4-flash \
    ../.venv/bin/python -m pytest tests_live/test_live_multi_runner.py -q
COLLIMATE_LIVE_PROVIDER=cerebras CEREBRAS_API_KEY=… COLLIMATE_LIVE_MODEL_CEREBRAS=qwen-3.8-27b \
    ../.venv/bin/python -m pytest tests_live/test_live_multi_runner.py -q
COLLIMATE_LIVE_PROVIDER=gemini GEMINI_API_KEY=… COLLIMATE_LIVE_MODEL_GEMINI=gemini-flash-latest \
    ../.venv/bin/python -m pytest tests_live/test_live_multi_runner.py -q
```

What this file needed from the harness, beyond the agent-surface suite:

* **The tree path is the exact way to a run's tab.** With two runs in one
  cell the Files pane holds two `_runs/<label>/` folders. `CellView.open_path`
  shows the pane ("Show files"), opens each folder on the path by its
  `data-filenode-path` and clicks the file's row — the user gesture that
  opens the tab and activates it.
* **The Status view is read by row** (`CellView.open_runs`, `runs_rows`,
  `open_run(run_id)`, `row_buttons(run_id)`, `click_stop(run_id)`,
  `messages(run_id)`, `open_all`): each live run has its own row, so a
  scenario names the run it means; `focus_tab` / `tab_selected` find a tab by its title (`<path>`,
  or `<path> — <runner> <id>` on a locked file) and `showing(path)` finds a
  renderer host by its `data-file-path`.
* **The canvas is read from `/api/state`** (`LiveApi.main_stacks`,
  `stack_index_of`, `state_files`) — the clone's place right of its source
  is a fact of the canvas, not of any cell endpoint.
* **A red scenario keeps every started run's events and artifacts** in the
  evidence dir (`<test>.<run id>.events.json` / `.artifacts.json`), read
  before the teardown stops the runs — the server log holds HTTP lines only.

**First pass: 2026-09-26** — all five scenarios of that day green on each
of the three providers, in headless Chromium through the in-process
pydantic-ai harness: OpenRouter `deepseek/deepseek-v4-flash` (5 passed in
2m58s with two of them red on the file's own walrus bug, then 2 passed in
33s on the fix; 5/5 again in the final full-tree run), Cerebras
`qwen-3.8-27b` (5 passed in 1m48s; a reasoning model, its answers follow a
reasoning block) and Gemini `gemini-flash-latest` (5 passed in 2m11s). A
scenario takes 13–15s, almost all of it the two runs' spin-up and the page
reload. No product change was needed: the first
run's two failures were a bug in the file itself (a walrus inside a
conditional expression), fixed the same day. Evidence of the pass:
`tmp/multi-runner/WP-F3/run{4,5}-mr-openrouter`, `run6-mr-cerebras`,
`run7-mr-gemini` (each `pytest.log` plus `pytest-of-michael/*/evidence/`).

## Codex on the host — `test_live_codex_host.py`

The shipped Codex chat config placed on the host (`docker: none`). The chat
generator runs under the process manager's rlimits, and codex-acp, this
machine's own `codex` and anything codex starts for a turn inherit them; the
scenario asks that chain to read a workspace file and checks the reply, and
the run's record for a process that died at startup. Added after the
2026-10-10 crash, where the generator's address-space cap killed Codex's V8
code-mode host before its first tool call. Gated on `codex` on PATH and an
OpenRouter key (environment or OS keychain); the model is `OPENROUTER_MODEL`
on a codex model entry. Under OpenRouter the model calls `read_file`
directly; the code-mode host itself is reproduced with `codex exec` under
`ulimit -v` (fails) and `ulimit -d` (reads), see
`docs/ai_tools/notes/2026-10-10/project_generator_memory_cap_rlimit_data.md`.

```bash
# codex on PATH, an OpenRouter key in the environment or the OS keychain
../.venv/bin/python -m pytest tests_live/test_live_codex_host.py -q
```

## How the harness is isolated

* **Its own server.** `conftest.server` spawns uvicorn on an **ephemeral
  port** (never 5001 — a dev session or another agent may own it) with
  `cwd=<pytest tmp dir>`. The data dir IS the CWD (`main.cli_main`'s
  contract: `.collimate/` is CWD-relative), so the
  developer's own database is untouched. The bundled trees (`content/`,
  `builtins/`, `module-snapshot/` beside `api/`) and the built SPA
  (`api/static/`) resolve from the api MODULE directory and are found
  regardless of CWD.
* **The SPA is required.** The tests drive renderers, which are fetched from
  the API origin — a vite dev server cannot host them. Missing
  `api/static/index.html` skips the whole suite with the build command in the
  reason.
* **A keyring that stores, and forgets.** Unlike the `run-app` skill, the
  suite does *not* force `PYTHON_KEYRING_BACKEND=…null.Keyring`: every account
  a scenario creates saves its secret through the server's keyring, and the
  null backend refuses each save with a 503. Nor does the server get the real
  OS keychain: the secrets are the developer's own (the OpenRouter key, the
  Claude login's access token), and a server the teardown SIGKILLs, or a
  pytest that crashes before deleting its accounts, would strand them there
  as orphan slots. So the server always runs on the in-memory
  `_memory_keyring` (only the server process reads its keyring; runs get the
  resolved secrets as env). The OS keychain is only READ, by the pytest
  process in a bounded child, for an `OPENROUTER_API_KEY` not in the
  environment; when it does not answer (no unlocked D-Bus session) that gate
  skips, saying so.
* **One browser page at a time.** Two views on the same chat make the second
  one take the single-attach 4409 `busy` state, which would measure the wrong
  thing.

## Driving traps this harness already encodes

* **The terminal renders to a CANVAS.** xterm.js paints into
  `.xterm-text-layer` / `.xterm-cursor-layer`; there is no `.xterm-rows` DOM,
  so terminal OUTPUT is invisible to the driver. (The 2026-08-29 QA round's
  terminal dumps were 0-byte files for exactly this reason.) Readiness is
  measured structurally — `CellView.terminal_mounted()` — and everything the
  TTY *said* is asserted on the run's terminal transcript
  (`_agent/terminal/transcript.json`, a run log artifact read with
  `api.run_log` once the run has ended — it is filed at the run's flush, not
  while the TTY runs), which is the contract anyway. `CellView.terminal_text()` is kept, documented as evidence-only.
* **The canvas is a transformed scene.** A cell can be in the DOM far outside
  the viewport; `CellView.open()` walks the stack strip with the app's own
  `Control+ArrowRight` until the box is on screen, then clicks twice (the
  focus scrim eats the first click).
* **A fresh page mounts only the cell's PINNED file** — the run's own
  surface pointer. When the chat renderer is not the one mounted,
  `open_chat_tab()` opens `conversation.chat` from the file tree.
* **Toolbars live in shadow roots too.** A web tab's Capture and New tab
  are clicked inside the tab's renderer host
  (`click_tab_button(path, label)` — Playwright's CSS engine pierces the
  shadow root). Each posts its action with the surface file as its source.
* **The composer lives in a shadow root** and is a controlled React
  textarea: `fill`/`type` do not reach it, so `send()` uses the native value
  setter plus a bubbling `input` event, then clicks Send (waiting for the
  button, which churns while a turn streams).
* **Starting a container can kill an in-flight renderer fetch.** Docker
  flipping the host's network makes Chromium abort with
  `ERR_NETWORK_CHANGED`, and the shadow host is then permanently empty
  ("NativeRenderer mount failed"). `open_chat_tab()` reloads once and
  retries; `send()` and `pick()` route through it, so every composer
  interaction is self-healing.
* **Never `networkidle`** — the per-channel SSE stream never closes.
* **The composer's selects are session MODES only.** A run's model and
  harness are its account's, so `CellView.pick()` drives the mode select and
  nothing else; a config change the session does not own is refused.
* **claude's own records arrive at the run's end.** The harness home is a
  disposable temp dir; its nonsecret session state is copied to
  `<cache>/collimate/agent-state/<cell id>/claude-code` when the harness
  closes, so `claude_records()` is read after `wait_finished`, never
  mid-run. The workspace path it keys on comes from the run's
  `_agent/session.json`, a run log artifact.

## Known limitations

* **Container reaping is global *for this suite*.** A Collimate server
  reaps, at boot, any container labelled `com.collimate.run_id` that was
  created before it started — but normally only its own, because every
  container it creates is stamped `com.collimate.instance=<id derived from
  the server's data dir>` and a foreign stamp is spared (`api/instance.py`).
  That ownership rule assumes the data dir outlives the process, and ours
  does not: the fixture runs uvicorn in a fresh pytest tmp dir every
  session, so each run's containers are stamped with an id no later server
  will ever claim — and a session that ends in the teardown's SIGKILL, which
  skips the lifespan's cleanup, would strand them permanently. So the
  fixture sets `COLLIMATE_REAP_ALL_INSTANCES=1` in the server's environment,
  which restores the pre-stamp behaviour: sweep every pre-boot
  `com.collimate.run_id` container and every `collimate-sandbox-*` one, no
  matter who made it. The consequence is the old one — booting this suite
  while ANOTHER Collimate server has a live container-backed run will kill
  that run's container. Run the docker scenarios when no other session has
  one in flight.
* **The SDK and the content are live.** `collimate-sdk` resolves to the
  `sdk/` workspace member (`[tool.uv.sources]`) and a new channel seeds the
  generators and renderers from the checkout's `content/`, so the suite
  runs the checkout's current SDK and content.
* **Timing.** The claude scenarios run about 20–60 s each and the container
  terminal runs about 90–100 s with a warm image. A COLD image (first
  `COLLIMATE_LIVE_DOCKER=1` run) builds it first, which is minutes and
  network — every individual wait is bounded, the wall clock is not.
* **The suite writes into one channel** ("Live Agent Surfaces") but a fresh
  CELL per scenario, so tests stay independent while a session pays for one
  channel seed. The session captures the UUID returned by creation; the label
  does not address requests or recovery. The accounts it creates live in the server's own registry
  (`COLLIMATE_ACCOUNTS_CONFIG`), never the developer's.

## Evidence

Failures write into `<basetemp>/evidence/`: a full-page screenshot, the
browser console log, and the tail of the server log, all named after the
test. Point `--basetemp` somewhere you can read to collect them:

```bash
COLLIMATE_LIVE_CLAUDE_CODE=1 ../.venv/bin/python -m pytest tests_live -q \
    --basetemp=/tmp/live-run
```

Scenarios also drop named screenshots at their interesting moments
(`…answered.png`, `…terminal-ready.png`) — those are written on
every run, pass or fail. Since 2026-09-26 a failed scenario also keeps every
run it started — `<test>.<run id>.events.json` (the run's event log: the
generator's log lines, phases, stop/finish) and `.artifacts.json` — read
BEFORE the teardown stops the runs; the server log tail holds HTTP lines
only, which is why the first opencode failure of the 2026-09-26 pass could
not be diagnosed from it.

## Adding a scenario

* its own cell (a wedged session must never reach the next test);
* every wait bounded, with a label saying what was awaited (`api.wait_until`);
* click the status pane and type in the composer — reach for the API only where
  the browser genuinely cannot (a message's exact read state and thread, which
  the status pane draws but does not expose as data);
* pass the run id to any wait that expects a LIVE session
  (`api.wait_assistant(..., run_id=run)`) — a crashed generator then fails in
  seconds with its own reason instead of timing out on the symptom;
* open the cell with `open_cell(cell_view, cell)` (a reload) whenever the
  scenario touches the status pane — a page only learns about a run it did not
  itself start by loading again;
* make the runner with the `runners` fixture and the model with a gate
  fixture, so both are cleaned up without a `finally` in the test.

Related: `sdk/tests_live/` (the generator-level live suite),
`docs/ai_tools/notes/2026-10-11/project_live_api_tests.md`,
`docs/ai_tools/notes/2026-10-11/project_agent_surface_polish.md` (the Playwright traps
this harness encodes).

## D23 Pi/OpenCode/Cline/Codex raw native evidence in the app worker

After fixture review and an exclusive live-test runtime window, select the
existing real-app command scenarios with `COLLIMATE_LIVE_GENERATOR_HARNESSES=pi,opencode`
and `COLLIMATE_LIVE_DOCKER=1`, and set `COLLIMATE_LIVE_SDK_SOURCE` to this repo's
`sdk/` folder. Run `tests_live/test_live_generator_tool.py::test_real_agent_generator_command_publication_and_approval_D23`
from `api/` using the configured API interpreter and the normal private provider
setup described above. The selector collects ask and allow for both harnesses;
it does not itself authorize provider spending. Credentials belong in the
normal secret setup, never command transcripts. PAI/Goose defaults remain unchanged.

These native harnesses use a test-only copy of the app's accepted agent
generator package (the Agent cell's `agent/` folder plus its shared `_*`
folders) on its chat surface, admitted by
the normal package preview/Add routes. The original module,
config and asset bytes and PEP-723 dependencies are retained. To make Add retain
all shared files, their byte-identical folders are placed within the synthetic
package; its receipt maps every accepted path to the materialized test path.
Only the private manifest identity/name/entrypoint and extra observer files
differ. This is an instrumented accepted-source test package, not the unchanged
shipped source layout.

The worker observes its actual ACP transport and stores bounded, selected-key
scrubbed `_agent/d23-native/turn-NNNN.json` log artifacts. The same cached native
transport gets a fresh capture each turn. Full native prompt/session/tool IDs,
arguments, agreeing native result representations and matching `end_turn` are
checked with the SDK test-only strict decoder. Transport callbacks perform no
host IPC or file writes. Existing actual command cards, two immutable host
versions, typed invalid-file refusal, child grant/private config, observed
container identity/mount, cleanup and parent-liveness checks remain required.
A missing or unfamiliar native rejection encoding fails and must be diagnosed;
it is not replaced by a fabricated response. Offline transport/package checks
prove instrumentation only; SDK recording publication is not actual app proof.

Cline is separately opt-in with `COLLIMATE_LIVE_GENERATOR_HARNESSES=cline`, pinned
to Cline3.0.65/mcp-remote0.2.4. Its exact native display title includes the
source-defined truncated JSON arguments; full arguments and results are checked
from the raw artifact. The title decoder admits only empty settings or the
fixture's seven known ASCII string fields. Unsupported settings or Unicode
fail clearly. First execute the reviewed ask selector once to retain actual
rejection and new authorized approval evidence; unfamiliar native refusal
ordering/encoding fails and requires measured diagnosis. Allow is a later
coordinated selector, with no automatic retry. No Cline actual-app pass is
established by the offline extension controls.

Codex is separately opt-in with `COLLIMATE_LIVE_GENERATOR_HARNESSES=codex`, using
the reviewed `codex: 0.157.0` and `codex-acp: 1.13.1` fixture pins. The ACP
adapter bundles native CLI0.156.1; its evidence is distinct from the terminal
CLI0.157.0. The strict ACP decoder requires the actual
`mcp.collimate.test_generator` identity, full native `{server, tool, arguments}`
wrapper, agreeing text/structured result, and matched normal `end_turn`.
The unchanged worker and actual app scenario preserve source/run/cell/native
IDs, command cards, no child before approval, private child authority,
two immutable host versions and full cleanup. First run the reviewed ask
selector once after fixture review and an exclusive runtime window. Unknown
native permission or refusal encoding remains a measured failure; no automatic
answer or retry is added. Allow follows separately after root coordination.
Offline extension controls and the prior SDK ACP pass do not establish a
Codex actual-app pass.

Qwen is separately opt-in with `COLLIMATE_LIVE_GENERATOR_HARNESSES=qwen-code`,
pinned to Qwen0.23.3. Its strict decoder requires the measured
`test_generator (collimate MCP Server)` pending/progress identity, full semantic
arguments, agreeing complete raw/text result and matching normal `end_turn`.
The preceding failed `ReadMcpResource` lookup, when present, is retained and
classified with its exact native ID, metadata, URI and failure result. Every
target and ancillary display ID/title/status must match the captured native
updates; arbitrary additional calls fail. ASCII inputs and empty or the seven
reviewed app settings are required by the source-derived native JSON title.
All actual command-card, child/private-authority, two immutable host-version,
typed-refusal, container and cleanup assertions remain required. First execute
the reviewed ask selector once in the coordinated runtime window; unknown
native approval/refusal ordering or encoding remains measured failed evidence.
Allow follows separately. Offline controls and the SDK recording-host witness
do not establish actual app publication or a Qwen actual-app pass.
