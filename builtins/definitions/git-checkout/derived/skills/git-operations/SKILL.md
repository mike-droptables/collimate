---
name: git-operations
description: Run git against a Git checkout folder that has no .git (diff, log, show, blame, compare with the remote), and bring in the remote's changes or publish edits through the Git runners. Use before any git command on a folder that has a <folder>.git-checkout.json beside it at the cell root.
---

# Git for a checkout without .git

## Recognise a checkout

A cell never holds `.git`. When the Git Clone / Update runner brings a repository into the folder `<folder>/`, it writes `<folder>.git-checkout.json` at the cell root, beside the folder:

```json
{
  "about": "Git checkout record: …",
  "remote": "https://github.com/octo/api.git",
  "branch": "main",
  "commit": "3f786850e387550fdab836ed7e6dc881de23001b"
}
```

- `remote`: the repository's clone address (https://host/path, or ssh://user@host/path or user@host:path for a Git server reached over SSH). It never carries a user name, password or token.
- `branch`: the remote's branch the folder follows.
- `commit`: the full SHA of the remote commit that the folder's files are based on. The folder holds that commit's files plus the edits made in the cell since. Every difference between the folder and this commit is a local edit, and Git Push would send it. Paths the runners keep in Git only are not in the folder: symlinks, submodules, files over 20 MB, and anything under `node_modules`, `.venv` or `__pycache__`.
- `about`: a fixed note pointing here.

The folder's name is the record's file name without `.git-checkout.json`. A file with that suffix inside a folder is ordinary repository content, not a record. A folder with no record beside it is not a checkout, even if its files came from a repository. A record whose folder is gone is left over, and the next Git Clone / Update into that name clones afresh.

`git status` or `git diff` inside the folder fails with "not a git repository". That is expected: never fix it by creating `.git`.

## Run git against the recorded commit

Work in a temporary blobless clone in the system temp folder, never in the cell. Give git the cell folder only as a work tree it reads, with an index file outside the cell. Do the whole job in ONE shell command: variables and functions may not survive between your commands.

Read the record first and put its values in the first four lines, quoted. Run from the cell root, your shell's starting folder:

```sh
set -eu
FOLDER='api'                                          # the record's file name without .git-checkout.json
REMOTE='https://github.com/octo/api.git'              # "remote"
BRANCH='main'                                         # "branch"
COMMIT='3f786850e387550fdab836ed7e6dc881de23001b'     # "commit"
CELL="$PWD"
if [ -n "${COLLIMATE_GIT_SSH_BASE:-}" ]; then case "$REMOTE" in "$COLLIMATE_GIT_HTTPS_BASE"*) REMOTE="$COLLIMATE_GIT_SSH_BASE${REMOTE#"$COLLIMATE_GIT_HTTPS_BASE"}"; REMOTE="${REMOTE%.git}.git";; esac; fi
case "$REMOTE" in ssh://*|*@*:*) [ -n "${GIT_SSH_COMMAND:-}" ] || { echo "$FOLDER/ is reached over SSH and no SSH key reached this harness (the Git account is not an SSH account, or this harness runs in a container): ask the user to run Git Clone / Update or Git Push" >&2; exit 1; };; esac
WORK="$(mktemp -d "${TMPDIR:-/tmp}/git-checkout.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
trap 'exit 130' INT TERM
case "$WORK/" in "$CELL"/*) echo "the temp folder is inside the cell; set TMPDIR to a folder outside it" >&2; exit 1;; esac
export GIT_TERMINAL_PROMPT=0 GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
R() { git --git-dir="$WORK/repo.git" "$@"; }
G() { GIT_INDEX_FILE="$WORK/index" git --git-dir="$WORK/repo.git" --work-tree="$CELL/$FOLDER" "$@"; }
git clone --quiet --bare --filter=blob:none --single-branch --branch "$BRANCH" --no-tags \
  -- "$REMOTE" "$WORK/repo.git"
R config core.fileMode false
R config core.autocrlf false
if ! GIT_NO_LAZY_FETCH=1 git --git-dir="$WORK/repo.git" merge-base --is-ancestor "$COMMIT" "$BRANCH" 2>/dev/null; then
  echo "note: the remote's $BRANCH no longer contains $COMMIT (its history was rewritten)" >&2
  R fetch --quiet --filter=blob:none origin "$COMMIT" \
    || { echo "the remote no longer serves $COMMIT; tell the user to run Git Clone / Update" >&2; exit 1; }
fi
R ls-tree -r "$COMMIT" | awk '$2 == "blob" { print $3 }' > "$WORK/blobs"
if [ -s "$WORK/blobs" ]; then
  R -c fetch.negotiationAlgorithm=noop fetch --quiet --no-tags --no-write-fetch-head \
    --filter=blob:none origin --stdin < "$WORK/blobs"
fi
G read-tree "$COMMIT"
G add -A -- . ':(exclude,glob)**/node_modules/**' ':(exclude,glob)**/.venv/**' ':(exclude,glob)**/__pycache__/**'
G diff --cached --stat "$COMMIT"      # then any command from the table below
```

- When the runner binds a token Git account, your environment already carries `GIT_CONFIG_COUNT=1` with an `http.<server>.extraHeader` for that account's server; git uses it by itself, and the clone above authenticates. Never set, override, print or unset `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_0` or `GIT_CONFIG_VALUE_0`.
- When the runner binds an SSH-key Git account and your harness runs on this machine, your environment already carries `GIT_SSH_COMMAND` (with `GIT_SSH_VARIANT=ssh`): ssh with the account's key, the host key of the account's own server only, and no ssh config or agent. git uses it by itself for an `ssh://` or `user@host:path` remote, and the clone above authenticates. For a GitHub or GitLab account, `COLLIMATE_GIT_HTTPS_BASE` and `COLLIMATE_GIT_SSH_BASE` name the account's https address and its SSH address; the setup's `if` line turns a record under that https address into the SSH address the Git runners use (without the https port or base path), and leaves any other address as it is. Fill `REMOTE` with the record's address and never rewrite it to SSH yourself. Never set, override, print or unset `GIT_SSH_COMMAND`, `GIT_SSH_VARIANT` or the two `COLLIMATE_GIT_*` names. Without `GIT_SSH_COMMAND`, the setup stops at an SSH remote with a message to the user.
- The `trap` removes the temporary clone when the command ends.
- The clone is blobless; the `ls-tree … fetch --stdin` step then downloads the recorded commit's files in one batch. Without it, `G add` fetches each file separately when the repository's `.gitattributes` sets `text`. Run the setup again for the next question rather than keeping `$WORK`.
- The two `R config` lines and the exported `GIT_CONFIG_NOSYSTEM`/`GIT_CONFIG_GLOBAL` settings are the Git runners' own: no global or system git config, no file-mode or line-ending conversion. `G diff` then lists exactly what Git Push would send.
- `R` runs git on the temporary clone. `G` runs it with the cell folder as the work tree and the temporary index. Paths are relative to the folder.

| Question | Command after the setup |
|---|---|
| What did the cell change since the recorded commit? | `G diff --cached --stat "$COMMIT"`, then `G diff --cached "$COMMIT" -- <path>` |
| Which files are new, changed or deleted? | `G diff --cached --name-status "$COMMIT"` |
| The recorded commit | `R show --stat "$COMMIT"` |
| History of a file | `R log --oneline -20 "$COMMIT" -- <path>` |
| A file as it was at the recorded commit | `R show "$COMMIT:<path>"` |
| Who wrote each line (lines edited in the cell show as "External file (--contents)") | `G blame --contents "$CELL/$FOLDER/<path>" "$COMMIT" -- <path>` |
| What the remote's branch gained since the record | `R log --oneline "$COMMIT..$BRANCH"` and `R diff --stat "$COMMIT" "$BRANCH"` |
| Would the remote's changes touch the files the cell edited? | compare `G diff --cached --name-only "$COMMIT"` with `R diff --name-only "$COMMIT" "$BRANCH"` |
| Another branch | `R fetch --quiet --filter=blob:none origin "refs/heads/<other>:refs/heads/<other>"`, then compare with `<other>` |
| Search the code at a commit | `R grep -n '<pattern>' "$COMMIT" -- <path>` |

- Commands that read many old file versions fetch each missing file from the remote as they go: `log -p`, `log -S`, `blame` on long histories, `grep` over a whole commit. Keep them to the paths you need. For many of them, leave `--filter=blob:none` out of the clone.
- Paths kept in Git only are absent from the folder, so they show as deleted in `G diff`. They are not edits, and Git Push never deletes them. `R ls-tree -r "$COMMIT"` shows mode `120000` for a symlink and `160000` for a submodule.
- Files the folder's `.gitignore` ignores are not added. Git Push never sends them either.
- Through `G`, run only `read-tree`, `add`, `diff`, `blame`, `grep` and `ls-files`: they write only inside `$WORK`. Never run `checkout`, `switch`, `restore`, `reset`, `stash`, `clean`, `merge`, `rebase`, `pull`, `apply`, `am`, `cherry-pick`, `revert`, `rm` or `mv` through it. They would rewrite the cell's files behind the Git runners.

## Bring in the remote's changes, publish edits

You cannot run these runners yourself. Tell the user which runner to run in this cell and with which settings.

- **Bring in the remote's changes:** the user runs **Git Clone / Update** in this cell, with the repository as its prompt. Folder is `<folder>`, or blank to find the folder by its record. The runner merges the remote's new commits into the folder's edits and moves the record's `commit`. A conflict changes nothing and names the files. Show the user the overlapping changes with the table above, for example `R diff "$COMMIT" "$BRANCH" -- <file>` next to `G diff --cached "$COMMIT" -- <file>`.
- **Publish:** the user runs **Git Push** in this cell. It needs the runner's Git account, with a credential that can write the repository. It sends the folder's edits as one commit on the recorded commit, merging the remote's new commits first when the branch moved, and it never force-pushes.
- **Propose changes for review:** the user sets Push to branch to a new branch name, and the run's note carries the compare link when the remote is on GitHub or GitLab. Then:
  - On GitHub, with `gh` installed and `GH_TOKEN` (or `GH_ENTERPRISE_TOKEN`) set: `gh pr create --repo <host>/<owner>/<name> --head <new-branch> --base <branch> --title '…' --body '…'`.
  - On GitLab, with `glab` and `GITLAB_TOKEN`: `glab mr create -R <group/project> --source-branch <new-branch> --target-branch <branch> --title '…' --description '…'`.
  - Otherwise give the user the run note's compare link.
- **To make edits**, change the folder's files with your file tools. That is all Git Push needs.

## Never

- Never create `.git` anywhere in the cell: no `git init`, `git clone`, `gh repo clone`, `glab repo clone` or `git worktree add` there, and no `--git-dir` inside it.
- Never commit, tag or push from the temporary clone. Git Push is how edits reach the remote, so the record always names a commit that is on the remote and nothing is force-pushed. A commit you pushed yourself would leave the record on the wrong base for the next Clone / Update or Push.
- Never edit, delete or hand-write the record. With a different `commit`, the runners treat the folder's files as edits against another commit, and a push could undo other people's work or bring back deleted files. Change branches with the runners' Branch and Push to branch settings.
- Rename or move the folder only when the user asks, and then always together with its record (`api/` and `api.git-checkout.json` become `web/` and `web.git-checkout.json`). Otherwise the runners stop recognising it as a checkout.
- Never print, echo, log or write `GIT_CONFIG_VALUE_0`. Never run `env`, `printenv` or `set -x`. Never put a credential on a command line, in `git config`, in a file or in the cell.
- Never print `GITHUB_TOKEN`, `GH_TOKEN`, `GH_ENTERPRISE_TOKEN` or `GITLAB_TOKEN`; never put any credential in a URL (`https://…@host`); never ask the user to paste a token or an SSH key into the chat; never add `-c http.sslVerify=false`, `GIT_SSL_NO_VERIFY` or `StrictHostKeyChecking=no`.
- Never read, copy, print or move the key file that `GIT_SSH_COMMAND` names, or the known_hosts file beside it. Never add the key to `~/.ssh` or to an ssh agent (`ssh-add`). Never change the ssh options of `GIT_SSH_COMMAND`, run `ssh` with other options, or set `StrictHostKeyChecking=no`.
- Never leave the temporary clone behind: keep the `trap`, or run `rm -rf "$WORK"` when done.

## Without the Git account, network or git

- Neither `GIT_CONFIG_COUNT` nor `GIT_SSH_COMMAND` is set: the setup clones anonymously, which works for public repositories. For a private repository it fails with "Repository not found", "could not read Username" or GitLab's "HTTP Basic: Access denied". Stop and tell the user that git on this private repository needs a Git account from Key manager bound to this runner's optional Git account role.
- `GIT_CONFIG_COUNT` is set but the server refuses the header (401, 403 or "not found"): the token cannot read this repository. A fine-grained GitHub token must list it, an organisation may require SSO authorisation for it, and a GitLab token needs `read_repository`. Tell the user; do not retry without the header.
- The remote is an SSH address (`ssh://…` or `user@host:path`) and `GIT_SSH_COMMAND` is not set: the Git account is not an SSH account, or this harness runs in a container, where the key does not reach. The setup stops at once with a message to the user; only the Git runners reach the repository. (A record's https remote has no `@`, so `*@*:*` matches only the scp form.)
- `GIT_SSH_COMMAND` is set but ssh fails: "Host key verification failed" means the server is not the Git account's own (the key reaches only that server, on port 22 for GitHub and GitLab), or its host key is neither one Collimate ships nor one pasted in the account's Known hosts; "Permission denied (publickey)" means the key cannot read this repository. Tell the user; never work around it.
- `GIT_SSH_COMMAND` is set and the record's `remote` is an https address the clone cannot read: the address is not under `COLLIMATE_GIT_HTTPS_BASE` (or that is unset: the account is a Git server, reached over SSH only at `ssh://` or `user@host:path` addresses), so the key does not apply to it and git tried it anonymously. Tell the user; never rewrite the address to SSH yourself.
- No internet ("Could not resolve host"): say that the runner's internet permission must allow the remote's host (the host in `remote`); SSH needs internet: full, because it cannot pass the web proxy.
- No git (`git --version` fails), or git older than 2.31 (which ignores `GIT_CONFIG_COUNT`, so a private repository reads as not found): report it and stop. You can still read and edit the folder's files, and the Git runners do their own git work.
- The commit is gone (the setup prints "the remote no longer serves …"): the branch's history was rewritten on the remote. Tell the user. Git Clone / Update explains how to recover.
