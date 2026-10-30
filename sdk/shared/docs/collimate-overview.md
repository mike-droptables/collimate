# Collimate — orientation and the working loop

Collimate is a local workbench for exploring alternatives with language models,
comparing their files side by side, and keeping a recoverable record of your
choices. The working rhythm is **explore, challenge, compare, merge, stop**.
Models help map the options; you decide which trade-offs to accept.

For the reasoning behind that approach, read `why-collimate.md`. This overview
explains the workbench and how the Guide grounds its answers.

## The pieces of the workbench

| Concept | What it means |
|---|---|
| **Channel** | A workspace containing stacks. |
| **Stack** | An ordered column of cells. Its front cell appears on the canvas; the stack head's pips, fan-out and the overview let you examine the others. |
| **Cell** | A named bundle of files. Edits and runs produce recoverable versions in the files pane's History; the cell's journal records its runs, edits, duplicates, merges and splits. |
| **Pinned file** | The file a cell shows on the canvas. You choose it; its renderer supplies the view. |
| **Generator** | A Python program that works on the cell where its run starts. It may use a model, a command or ordinary code. |
| **Runner** | A saved invocation of a generator: settings, prompt, supplemental files, security grant and account selections. |
| **Renderer** | A view for a file type, such as a document, diagram, image, chat or terminal. |
| **Handler** | A declaration of file types, with the checks (verifier) and authoring skills of the build in use. Metadata may also name a renderer alias. |
| **Capabilities** | The one stack that holds every generator, runner, renderer and handler the channel uses. Which of its cells holds one is only organisation. Its first face, **Manage**, lists and edits them all. |

Nothing runs on the Capabilities stack. A run works within its own cell
elsewhere: it cannot inspect or edit the Capabilities cells or invoke another
saved runner. The handler catalog supplies file-type metadata and the
authoring knowledge in use without exposing those cells' source files.

A run keeps its temporary interface files in `_runs/<label>/`. The host logs and
removes that folder when the run ends. A run's surface shows the moment the
run is accepted, in a starting state that already takes your input. Its files
belong to the run while it is live; other runs can read them. The chat
transcript follows the connected harness; past messages are read-only. Several
runs can work in one cell, so run in a new cell (the runners panel's **in new
cell**, `Shift+Enter`) when you want an independent alternative.

## Finding your way around

- **Canvas:** create a cell with the top bar's **＋**, or drop files onto the
  canvas. Open a cell to inspect and edit its files. **Find file** on the files
  rail, or `Mod+P`, opens the cell's files panel to search its files and open,
  add, duplicate or delete one.
- **Commands pane:** `Mod+K` or `/` opens the Commands pane, docked over the
  canvas. It lists commands for the selected cell, its stack and the channel,
  and jumps to any cell on the canvas.
- **Runners panel:** **Run** (the play icon beside the selected cell's status
  light), `Alt+R`, the Commands pane's **Run…** or the status pane's **New
  run** opens the runners panel inside the cell. Select a
  saved runner: **Run ‘x’** runs it in this cell; **in new cell**
  (`Shift+Enter`) copies the cell's files into a new cell before starting the
  run. The panel only runs runners; with none saved it offers **Open
  Capabilities**.
- **Capabilities:** the **Capabilities** pill in the top bar shows or hides the
  Capabilities stack. Its **Manage** face lists the channel's generators,
  runners, renderers and handlers with search and kind filters; clicking one
  brings the cell that holds it to the front. Paging on (`Alt+↓` or the stack's
  pips) shows the built-in cards and then the cells themselves. A cell moves
  off the stack, onto the board, to set its capabilities aside, and moves back
  on to use them again; a cell with a run in progress cannot move on.
- **Runner editor:** Manage's Runners group adds, edits, duplicates and
  deletes runners. Choose the generator, load a named config, select model
  accounts, set the prompt and settings, and review Files and Security. A new
  runner asks which Capabilities cell keeps it.
- **Key Manager:** manage model accounts, logins and Git accounts (GitHub, GitLab or any Git server, each with the commit author its pushes use) shared across channels.
  A runner uses the account selected in its editor. The connection, model and
  grant determine which harnesses and commands that run can use.
- **Running cells:** live interfaces open as file tabs. The status light at the
  top left of a cell shows its runs, and the caret beside **Remove** in the
  header counts the live ones. Either opens the cell's status pane, docked on
  the cell's right (or bottom). The status pane lists the cell's runs (state,
  whether each reads messages), with finished runs dimmed below until **Clear
  finished**; click a run for its messages and the files it touched
  (**Default**), the same filtered to the file you last viewed (**Current
  file**), or its **Log**, with **Stop** in its header and in the top bar's runs
  menu. **All** shows every live run's messages, the cell's own and the cell's
  log, every run's lines merged by time. The composer writes a message,
  **Queued** or **Interrupt** (to cut the current turn short); **Add
  annotation** attaches a selection from the current file, and a message
  written on **All** waits in the cell until you send it to a run.
- **Comparing:** duplicate a cell, arrange rivals side by side, fan out a stack
  to see its cells together, or zoom out (`Alt+A`) to the overview, where every
  stack is a column of its cells. Its **Removed** shelf keeps every removed cell
  ready to restore, and a cell's **journal** lists its runs, edits, duplicates,
  merges and splits, with lineage arrows to the cells it came from and went to,
  so a choice can be revisited.
- **Keyboard:** every shortcut is a command in the **Commands** pane (`Mod+K`):
  search it by name or key, run it with Enter, or press Shift+Enter on it to
  bind a key of your own, which then works with the pane closed too.

The starter generator families include **Web**, **Git** (clone a GitHub, GitLab or other Git repository into a cell folder, update it and push your edits back), **Launch** and the **Agent**.
Their saved runners depend on what you selected when creating the channel and
what you have saved since. An Agent run works on the surface its **Surface**
setting picks: **Annotation** (the default: you post messages to the run,
queued or interrupting it, pointing at files and at elements within them; it
answers in place and edits the cell; a one-shot runs its prompt once and ends),
**Chat** (a conversation) or **CLI** (the harness's own terminal). With **Start
from the selected file** on, the selected file is its first message. Asked for
a summary, a run writes a `.llm_summary` file at the cell root that a later run
picks up from when asked.

## What the Guide knows

**Guide** is an Agent preset on the chat surface for orientation. It bundles only this overview and
`why-collimate.md`; it does not list saved runners or inspect installed generator
and renderer source packages. For questions about file types and authoring it
uses the current handler catalog:

1. `list_handlers` returns handler metadata, including types, examples,
   whether a build is in use, disabled state and clashes. A declared file type does not establish
   which saved runners or renderer packages are installed.
2. `handler_skills` returns the namespaced skills a handler has in use and
   their references. These carry the format's authoring and build instructions.
   Empty or unavailable skills are reported, rather than guessed from a build
   waiting for review or the Guide's memory.
3. `validate_file` checks an existing cell file with its applicable verifier.
   A structural verdict is evidence about that file's format, not proof of
   task completion or permission to execute it. Without an applicable verifier,
   a valid result does not establish a format contract.

The Guide can explain a handler's workflow and suggest actions for
you to take. It does not demonstrate or start runners, install packages, or
change the channel. If its tools cannot provide current evidence, it says what
is unavailable and answers only from the orientation material it has.

## Authoring and approval

Write a package in a working cell using the skills its handler has in use. A
`.generator` describes a generator, a `.runner` stores an invocation, a
`.renderer` describes a file view, and a `.handler` declares file types. The
catalog, rather than this overview, supplies their current authoring rules.
**Create…** in Manage asks what the channel should learn to do, checks what it
already has, and opens a work stack beside Capabilities with a brief of your
answers and the authoring guides; run any runner there.

You put a package into use with the files panel's **Add to Capabilities…**,
which previews it and adds it to a new or chosen Capabilities cell, or by
importing it, moving its cell onto the stack, or editing it in a Capabilities
cell. Renderer previews use a sealed frame. A handler's build makes its checks
and skills; nothing a build makes is used until the user reviews and accepts
it. Manage's Handlers group shows each handler's card with its status (such as
In use, or a build waiting for review) and its actions: Validate, Build…,
Review and accept, Add example and Edit. **Build…** opens a work stack beside
Capabilities with a copy of the handler and a brief; run any runner there, add
the package back from that stack's files panel with Replace, then review and
accept the build on the card. An agent's file output alone does not install a
package or accept a build.

The app's built-in handlers and renderers are the **Definitions**, **Live** and
**Text** cards right after Manage: the definition, options and Git checkout handlers, the
run, chat, terminal, website and webview surfaces, and the plain text view.
They cannot be removed, moved or edited; a channel can only add examples to a
built-in handler and regenerate its skills. **Regenerate skills…** on its card
opens a work stack the same way, and **Use these skills** in that stack's files
panel sends the result back to the card for review.

Merge and Split work on ordinary cells through an `.options_stack` declaration.
They are separate from expanding a stack into a fanned view. Keep the
alternatives and their evidence visible long enough to make a considered choice.
