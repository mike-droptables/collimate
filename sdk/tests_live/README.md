# `tests_live/` — the opt-in live suite

Everything here talks to a real model, a real subscription, or a real Docker
daemon. Nothing here runs by default.

```bash
uv run pytest tests_live            # the whole live layer
uv run pytest tests_live -q -k oneshot_text
```

`pyproject.toml` sets `testpaths = ["tests"]`, so a bare `uv run pytest` never
collects this tree — the separation is structural, not a marker you have to
remember to exclude. Every test is also marked `@pytest.mark.live`, which is
what lets you slice *within* the live run (`-m live`, or `-m "not live"` if you
ever point pytest here directly).

## Public SDK image permissions (unpaid)

The image regression runs against an already built command image, without a
provider or model request. Supply its full Docker image reference and run as
the non-root host user whose UID/GID commands use:

```bash
COLLIMATE_TEST_COMMAND_IMAGE=collimate-linux-small:0f66c6c76b1f \
    uv run pytest tests_live/test_live_forward_image.py -q
```

It checks actual same-UID imports, canonical facade/native hashes, root
ownership, public read/traverse permissions and absence of user write access.
Without the explicit image reference this opt-in check skips. The default
staging regression remains in `tests/test_forward_image.py` and never skips.

The unpaid command lifecycle checks use disposable containers with `sleep` as
PID 1. They require the supervisor, direct child and adopted descendant PIDs
to disappear after completion, cancellation and timeout:

```bash
COLLIMATE_LIVE_DOCKER=1 \
COLLIMATE_TEST_COMMAND_IMAGE=collimate-linux-small:0f66c6c76b1f \
    uv run pytest tests_live/test_live_generator_tool_processes.py -q
```

These process checks make no model calls or host publication claim. The app's
separate exact-child fixture verifies real host versions and typed refusals.

## What selects what

Nothing in this tree contains a secret. Which tests actually execute is decided
entirely by the environment.

| Variable | Effect |
| --- | --- |
| `ANTHROPIC_API_KEY` | runs the anthropic parametrization |
| `OPENAI_API_KEY` | runs the openai parametrization |
| `GROQ_API_KEY` | runs the groq parametrization |
| `CEREBRAS_API_KEY` | runs the cerebras parametrization |
| `OPENROUTER_API_KEY` | runs the openrouter parametrization |
| `GEMINI_API_KEY` | runs the gemini parametrization, the `antigravity` ACP conformance round trip and commands matrix, and the `antigravity` terminal round trip in its container (on a Gemini key agy's CLI runs in the harness container; on the Google plan it is this machine's agy under its own sign-in, `test_live_host_terminal.py`; the data-use decline is a setting of the Google account, made once on agy's own screen: Collimate records none and blocks no telemetry) |
| `COLLIMATE_LIVE_CLAUDE_CODE=1` | runs the claude-code tests against the **logged-in local `claude` CLI** (subscription auth — no key), and Claude Code's CLI surface on this machine (`test_live_host_terminal.py`) |
| `COLLIMATE_LIVE_HOST_ACCOUNT_CLAUDE_CODE=<entry id>` / `COLLIMATE_LIVE_HOST_ACCOUNT_ANTIGRAVITY=<entry id>` | the Key manager entry (its credential account id) whose per-account home under the real host cache (`~/.cache/collimate/host-homes/<harness>/<entry id>/`) holds a completed sign-in of that CLI: `test_live_host_terminal.py` then runs one short turn on it, billed to the subscription, instead of stopping at the CLI's sign-in screen in a fresh home (see the manual checklist below) |
| `COLLIMATE_LIVE_DOCKER=1` | runs the containerized-harness tests (**may build images on first run — minutes, and network**), including one turn per subscription login a `COLLIMATE_LIVE_LOGIN_<HARNESS>` variable names |
| `COLLIMATE_LIVE_LOGIN_ANTIGRAVITY=<path>` | the path (never the contents) of the antigravity-acp server's token document, `antigravity-acp/acp_token.json`, from a one-time sign-in through the official server in Zed or JetBrains (on macOS with `AGY_ACP_FORCE_FILE_STORAGE=1`); with `COLLIMATE_LIVE_DOCKER=1` it runs the `antigravity`/`google` leg of `test_plan_login_round_trip` on the Google plan with no key. The test copies the file 0600 into a lease under its tmp dir, as the host leases it to a run, and never writes the source (Google's token does not rotate; the run's refresh is thrown away with the copy). Unset, or naming no file, those legs skip naming the variable and how to make the file (the official server's sign-in — not the Key manager, which only takes a paste). The other plan harnesses read `COLLIMATE_LIVE_LOGIN_<HARNESS>` the same way (`OPENCODE`, `PI`, `CLINE`: the harness's own `auth.json` / `providers.json`), except that their refresh tokens are single-use: a refresh the run leaves in the lease is written back to the source, as the host commits it to the account, unless the source changed during the run. Point those at a copy you can lose |
| `COLLIMATE_LIVE_UNPINNED=1` | runs every harness at the release a user's run gets today: the images build their latest releases (not the `commands` tests' pinned opencode and qwen-code), and the host adapters (codex-acp, claude-agent-acp) float the way their preset runner's `packages:` asks |
| `COLLIMATE_LIVE_MODEL_<PROVIDER>` | override the text model for that provider |
| `COLLIMATE_LIVE_MULTIMODAL_MODEL_<PROVIDER>` | override the vision model for that provider |

The provider names and their env vars are exactly `collimate.providers.PROVIDER_KEY_ENV`:
every provider an API key bills (`mistral` among them, which `vibe` and
`copilot` can be pointed at).

## How a run is pointed at a provider

The agent reads its harness, provider, model, endpoint and credential from the
account bound to the `model` role — the config carries none of them. So every
live run binds one: `conftest.model_account(provider, …)` builds a model entry
from the provider's env key (`metadata.harness` is the backend; a CLI login is
`auth_type: subscription` plus the harness's `plan`, and carries that plan's
own token — `CLAUDE_CODE_OAUTH_TOKEN` for `claude_code` — instead), and

* a generator-level run passes it through `run_agent_generator(account=…)` /
  `run_chat_generator(account=…)`, which hand it to `run_generator` as
  `accounts={"model": …}`;
* an engine-level turn (`eng.run_agent` against a bridge fixture) binds it with
  `conftest.bind_model_account(…)` first — the engine resolves the key through
  `gen.account("model")` and will not fall back to an ambient one.

A run with no such account fails immediately with *No account bound for role
'model'*, which is exactly what the product does.

The Agent cell ships one generator, `agent`, whose `surface` setting picks
the run's one surface: `annotation` (one-shot or interactive —
`agent_config()`'s default is a one-shot), `chat` (`agent_config(surface="chat")`;
`run_chat_generator` always runs the chat) and `terminal` (CLI: a harness's
own TTY: in its container, or the vendor's CLI on this machine for Claude Code
and for Antigravity on the Google plan). A chat scenario hands its task in `messages=`
(`run_chat_generator` releases message N+1 once turn N's reply has landed);
the chat's prompt is only an optional opening message. An annotation
one-shot works from `prompt=`: the harness seeds it as the run's first
annotation message, as the host does, and the run posts its answer as a
reply to it (`res.annotations`). An interactive annotation run reads the
messages scripted with `annotations=`.

The **subscription** legs (`opencode`/`pi`/`cline` + ChatGPT, `opencode` +
GitHub Copilot, `opencode`/`pi` + SuperGrok, `antigravity` +
Google) are selected by a login document on this machine, named by path in
`COLLIMATE_LIVE_LOGIN_<HARNESS>`. A run reads a plan login only from its
account's lease, which the host writes for the run and collects after it
(`login_store.login_path`, laid out as `PLAN_LOGINS[harness].lease`); there
is no Collimate login folder on the machine to hold one. So
`test_plan_login_round_trip` binds the plan account, leases the named
document through the `plan_login_lease` fixture (which stands in for the
host's `materialize_account` / `release_account`), and runs the leg when the
harness's own readiness predicate finds that plan's login there. Otherwise it
skips, naming the missing variable and how to make the document with the
harness's own CLI (`plan_login_steps`). At the end the fixture does what the
host's release does with a refresh: a rotating login (every plan but
antigravity's Google one) that the run refreshed in the lease is written back
to the source file, since the refresh token the source held is now spent.
No test can create such a login — each is a device-code or paste-the-code
prompt a human answers once — and none of these runs is handed an API key,
so a green there is a turn the subscription paid for.

A provider with no key **skips**, with the missing variable named in the skip
reason. A keyless machine therefore reports "all skipped", never "all failed":

```bash
env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY -u GROQ_API_KEY \
    -u CEREBRAS_API_KEY -u OPENROUTER_API_KEY -u GEMINI_API_KEY \
    uv run pytest tests_live -q
```

## Default models

Text models come from the engine's own `PROVIDER_DEFAULT_MODELS`, so a lineup
rotation moves this suite with the engine rather than against it. Vision models
are declared in `conftest.py`. In the multimodal legs, a cerebras model that is missing from Cerebras's public catalog
(`api.cerebras.ai/public/v1/models`) SKIPS with the ids it does serve; the other
text legs fail on it. `test_live_model_defaults.py` fails when one of these
defaults goes stale:

| provider | text (engine default) | multimodal |
| --- | --- | --- |
| anthropic | `claude-sonnet-5-5` | `claude-sonnet-5-5` |
| openai | `gpt-6-astra` | `gpt-6-astra` |
| gemini | `gemini-3.8-flash` | `gemini-3.8-flash` |
| groq | `openai/gpt-oss-120b` | *(none — image tests skip)* |
| cerebras | `gpt-oss-120b` | `qwen-3.8-27b` |
| openrouter | `anthropic/claude-sonnet-5.5` | `anthropic/claude-sonnet-5.5` |
| xai | `grok-4.7` | *(none)* |
| mistral | `mistral-medium-latest` | *(none)* |

Turns run at the model's own sampling and output defaults.

## The files

| file | what it proves | cost, roughly |
| --- | --- | --- |
| `test_live_pydantic_ai.py` | one-shot + assistant-ui chat + file tools, per provider | ~4 short turns per provider, a few thousand tokens each |
| `test_live_multimodal.py` | attachments on a chat turn really reach the model: PNG, two images, GIF, and the drop of a file the host could not type | ~4 image turns per vision-capable provider; a 64×64 PNG is ~100 image tokens |
| `test_live_claude_code.py` | the Claude Agent SDK path incl. the streaming-input attachment branch | 3 `haiku` turns |
| `test_live_harness_conformance.py` | every harness in the registry against a scripted provider (`provider_mock.py`, reached at the provider registry's endpoint; no key billed): `initialize` agrees with the vendored ACP protocol matrix, a turn calls the collimate bridge's `list_files` and its result reaches the reply, `_agent/session.json` is written and a reopen `session/load`s it, and the `commands` grant at each level (deny, ask rejected, ask allowed, allow); for pydantic-ai, that a command runs inside its sandbox container under `docker: full` and on this machine under `docker: none` | ~5 scripted turns per harness, **plus first-run image builds** |
| `test_live_acp_conformance.py` | what a scripted provider cannot show: claude's host locus on a real key or subscription, a real model's reopen and cancel, antigravity on a real Gemini key (and its session meta, cancel and sign-in guard against a scripted Gemini endpoint), the plan logins, the cell's planted config and injected notes against the `commands` grant, the codex/goose/opencode host twins, and claude's chat leg against a local Anthropic mock | 1 short turn per harness with a key, **plus first-run image builds** |
| `test_live_restricted_grants.py` | the claude adapter under a restricted grant: grant-selected host placement really execs the machine's `claude`, a withheld shell is refused, and every allow option the `latest` adapter offers — on a file-write prompt and on an ExitPlanMode prompt (a plan-mode turn) — is a known per-call id or caught by the mode-switch filter | 4 `haiku` turns |
| `test_live_model_defaults.py` | the suite's cerebras defaults are still in Cerebras's public catalog | one unbilled catalog read |
| `test_live_host_terminal.py` | the CLI surface on this machine: Claude Code's and Antigravity's (Google plan) TTY is the machine's own CLI, in the cell's real workspace, with the leg's own environment and no Collimate credential on the Google plan, out of the entry's per-account home (0700, under the host cache), opening at its prompt or its own sign-in with none of the first-run screens Collimate answers, its process group gone after the leg; one real turn on a named signed-in entry | nothing in a fresh home; 1 short turn per CLI on a named entry |
| `test_live_claude_tty_commands.py` | Claude Code's TTY on this machine under `ask` (reject / allow) and `allow`: the native command outcome, the key only in the cell's private helper file, never in the CLI's argv or environment | 3 `haiku` turns on an Anthropic key |

## Notes for whoever runs this next

* **The recording bridge cannot place containers.** `collimate.testing`'s
  bridge answers `env_place` with a synthetic ack and `env_exec` with
  `rc=0, output=""` — ideal for structural unit tests, fatal here, because a
  containerized harness would "pass" with no container in existence. The
  `docker_env_bridge` fixture in `conftest.py` supplies the host half for real:
  build-if-missing on the content-addressed tag, `containers.run`, `exec_run`,
  and removal on teardown.
* **Image builds are the slow part.** Tags are content hashes, so the first run
  on a machine builds the shared `collimate-linux-small` base plus each
  harness's layer. Subsequent runs are cache hits until a Dockerfile changes.
* **`auth=claude_code` uses a replacing child environment.** The engine
  passes the selected subscription token to the spawned CLI without inherited
  `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`. It constructs the child environment
  from the required PATH, HOME and TERM plus explicit run values; it does not
  delete credentials from the calling process's `os.environ`. In the app the
  caller is an ordinary run process; here it is the pytest session. Harness
  configuration and credential files remain in the run's owned harness home.
* **A session's live files end up in the run log.** The surface pointers, the
  chat transcripts and the trace are the session's live files: the run end
  moves them into that run's log and clears them from the cell (agent-v2
  §9.1.8), so a test reads them back from `res.log_files`. What the run
  *produced* — the files the task wrote — stays in `res.files`, and an
  annotation run's replies are in `res.annotations`.
* **Chat turns swallow their own failures.** The agent's `on_turn` writes
  `_(error: …)_` into the transcript rather than failing the run — that is
  correct for a live chat and useless for a test, so the chat tests assert on
  the transcript text, not just on `res.ok`.
* **A killed session leaves no containers behind.** Every container the
  suite places (the bridge's, and the two tests that run one directly) is
  labelled `collimate.live.session=<pytest pid>`. Session start reaps the
  containers of any session whose pid is gone, and session end sweeps this
  session's own (`reap_live_containers` in `conftest.py`; checked by
  `test_live_container_reaper.py`). Normal teardown never relies on it.

## Codex/Qwen native terminal tool intake (unpaid)

The dedicated inventory probe pins Codex TTY 0.157.0 (ACP adapter 1.13.1 is a
separate artifact) and Qwen 0.23.3. It starts the actual terminal runners with a
synthetic credential, enters the native `/mcp` inventory UI, and checks the
leg's own tools under deny/ask/allow. It sends no model prompt and makes no
publication or paid tool-dispatch claim. Run it only in a coordinated Docker
window:

```bash
COLLIMATE_LIVE_DOCKER=1 uv run pytest tests_live/test_live_codex_qwen_terminal_intake.py -q
```

Native execution, mandatory host approval/refusal, child committed publication,
old-route expiry/new-route validity and actual-app mediation are separate owed
D23 gates. Native settings/schema and recording runner tests alone do not
establish those gates.

## Codex/Qwen generator-tool native witness capture (paid, not a gate)

`test_live_codex_qwen_generator_tool.py` stages the missing full native MCP
records. Its six `allow`, `ask-allow`, and `ask-reject` cases pin the same native
versions as the inventory probe. They use the real terminal runners, real SDK
inbox command cards, a declared child dependency, and the recording host's
committed cell snapshot. A recording commit is separate from actual app
publication. Native confirmations keep their existing policy and receive no automated
answer during initial capture. Pending native dialogs are retained as actual
evidence for later witness review; SDK command cards use their separate
public inbox.

These are **capture-only** cases. Codex's raw MCP result decoder and both
native stopped-turn shapes still require actual witness review before a paid
success/refusal gate can be accepted. Qwen's decoder currently follows pinned
installed-source conversion, rather than a retained paid MCP success witness.
The already accepted deny inventory remains separate; this capture does not
repeat it or claim a full four-grant matrix.

Only run a specific case after an explicitly coordinated runtime handoff and
paid-call authorization. Use a private evidence directory, an authorized
OpenRouter key supplied by the existing secret-loading wrapper, and an
exclusive mode0600 stdout/stderr log with streaming selected-key redaction:

```bash
COLLIMATE_LIVE_DOCKER=1 COLLIMATE_LIVE_D23_CAPTURE=1 \
COLLIMATE_LIVE_D23_EVIDENCE=/absolute/private/evidence \
COLLIMATE_LIVE_MODEL_OPENROUTER=deepseek/deepseek-v4-flash \
uv run pytest 'tests_live/test_live_codex_qwen_generator_tool.py::test_native_terminal_generator_witness_capture_D23[allow-codex]' -m live -q
```

Full native JSONL records (with selected-key scrubbing), exact supplied
arguments, actual cards/UI, and the committed snapshot are retained even on
failure. The fixture tears down its terminal/bridge in that case. Preserve a
failed capture and classify its native state before any changed fixture or
another call. No normalized preview, model claim, or workspace marker alone
establishes native completion.

## Cline installed native intake (unpaid)

Run `COLLIMATE_LIVE_DOCKER=1 uv run pytest tests_live/test_live_cline_native_intake.py -q` with the normal live-test environment. The fixture pins Cline 3.0.65 and mcp-remote 0.2.4 and uses only a synthetic key. It exercises the installed core's native settings parser, JSON-schema tools, real MCP reads/writes, protected HTTP-settings refusal, actual ACP discovery and authentication boundary, and idle terminal replacement sessions. ACP discovery is held until an ordered native cancellation barrier completes; the native aborted-signal check prevents provider generation. Catalog lookups may occur. These checks do not establish paid model behavior or real-app publication validation.

The native settings use the installed direct stdio `mcp-remote` client. Cline ignores ACP `mcpServers`; user-declared servers remain unsupported. ACP's selected key is scoped to its exec because the pinned ACP authentication path rejects the ordinary OpenRouter native file; the terminal uses its selected private native file. No account store is read by this unpaid fixture.

### Cline native command and credential gates

`test_live_cline_commands.py` collects the exact-pinned native TTY cases for
Cline: four native TTY grants and one idle allow TTY credential-file/argv/
environment check (its ACP grant rows are the conformance suite's). Use the approved selected key in
the private process environment and `COLLIMATE_LIVE_DOCKER=1`; set
`COLLIMATE_LIVE_MODEL_OPENROUTER=deepseek/deepseek-v4-flash` for the reviewed
owner model. Run only in the coordinated native window, after three fixture
reviews, through exclusive0600 scrubbed evidence. Never place the key in argv.

```sh
python -m pytest tests_live/test_live_cline_commands.py -v
python -m pytest "tests_live/test_live_harness_conformance.py" -k cline -v
```

The TTY grant oracle reads the exact new owned native SQLite session and
full persisted tool-use/result records. For ask, one sole pending exact native
call plus a fresh complete native panel for the full command permits one `y`
or `n`; no hidden local hub approval ID is invented, and cumulative echoed
text cannot authorize a response. Matching final messages and idle only
trigger the native `/quit` command. The actual same session must subsequently
persist `completed`, exit code0 and matching DB/manifest end metadata after
normal process exit. Failed/cancelled/idle status or forced client cleanup
never establishes completed execution.
The native conversation record and ACP model option must report the selected
model. Tests compare selected credential bytes privately, require0600 native
file/0700 directory, scan process argv/environments without putting the witness
in the scanner's argv/env, and check actual runner cleanup. The idle allow
case submits no model prompt. Pure oracle tests do not establish native execution or real app publication.

## Manual checklist: the CLI sign-ins on this machine (the owner's)

The CLI surface of Claude Code, and of Antigravity on the Google plan, is the
vendor's own CLI on this machine, signed in through the vendor's own browser
flow inside its terminal. No test can do that step: it is a person in a
browser. Do it once per Key manager entry, then the live suite and every run
reuse it.

1. Install the CLIs on this machine, on PATH outside Snap: `claude` (Claude
   Code's own installer) and `agy` (Google's Antigravity CLI release,
   `github.com/google-antigravity/antigravity-cli`). Key manager → Verify on
   the entry says "its CLI runs on this machine (<binary> <version> at
   <path>)" once it is found.
2. In Key manager, create the entry: Anthropic → Subscription (Claude Code,
   the token field may stay empty), or Gemini → CLI login → antigravity /
   google (the document field may stay empty; it serves Annotation and Chat
   only, at your own risk). Note the entry's id (the Key manager URL or the
   accounts file, `~/.config/collimate/accounts.json`).
3. Start an Agent runner on that entry with Surface **CLI**, `docker: none`
   (or `full`) and `commands: ask`.
4. Claude Code: the TTY opens at claude's prompt; type `/login`, choose
   "Claude account with subscription", open the URL it prints (click it in
   the terminal), approve in the browser; claude says it is logged in.
   Antigravity: the TTY opens at agy's sign-in; choose "Google OAuth", open
   the URL, approve; then answer agy's first-run screens yourself (the
   Terms of Service & Data Use screen is the Google account's own choice).
5. Stop the run and start a second one on the same entry: it opens signed in
   (no sign-in prompt). The sign-in lives in
   `~/.cache/collimate/host-homes/<claude-code|antigravity>/<entry id>/`.
6. For the live suite: `COLLIMATE_LIVE_CLAUDE_CODE=1
   COLLIMATE_LIVE_HOST_ACCOUNT_CLAUDE_CODE=<entry id>
   COLLIMATE_LIVE_HOST_ACCOUNT_ANTIGRAVITY=<entry id> uv run pytest
   tests_live/test_live_host_terminal.py -q -rA` runs one short turn on each,
   billed to the subscriptions.
