# Generators and named configs

The SDK ships the Agent cell's `agent` generator for model-powered work (its
Surface setting picks a document, a chat or a terminal), `git` for Git
repositories (GitHub, GitLab or any Git server) on this machine, `launch` for
commands, terminals and applications, and `web` for browser sessions and
capture. Each run reads and writes its own cell. The `_agent_shared`,
`_env_shared` and `_web_shared` folders hold their shared code;
`_agent_shared/session` is the Agent generator's runtime. `_env_shared`
holds `git_account.py` (the `git` account role, read once into a
`GitAccount`) and `git_run.py` (git run with no ambient config, credential
helpers or hooks, and the HTTPS header or SSH key a remote gets). The `git`
generator's three runners, Git Clone, Git Update and Git Push (`git/git/git.py`,
its Repository prompt read by `git/git/prompt.py`), run plain git on a
repository the cell keeps in a folder or its root with a real `.git`, which
the host keeps between runs. `launch` (`launch/launch/run.py`) has no
setting for where it runs: the runner's grant decides. Under `docker: none`
a one-shot command or a terminal runs on this machine in the cell's
workspace (app mode, an image, a build folder, sudo and a read-only mount
are refused there); under `docker: full` it runs in a container, which app
mode needs (`requires_docker_if: {mode: [app]}`); `docker: limited` is
refused. Its Terminal runner runs a shell in its own container, its Local
terminal runner one on this machine.

Each generator owns named configs with complete editable runner fields and
a `files/` tree. Loading a config copies it into a saved runner. Credentials
remain in Key Manager; a runner binds account roles to local account IDs. A
Key Manager Git entry is a token, an SSH key or a username and token, with
the commit author its pushes use; the repositories a run touches are the
runner's own settings.
The runner's `_skills` and `_mcp` folders travel with it into the run.

The host controls Docker placement, network access for a contained run, and
package updates. The Agent generator enforces its `commands` grant. A run can create a
package in its workspace; the user places approved packages in the matching
stack.

- [Generator contract](../../sdk/shared/docs/how-to-write-a-generator.md)
- [Runner contract](../../sdk/shared/docs/how-to-write-a-runner.md)
- [SDK API](../../sdk/shared/docs/api-reference.md)
- [Testing](../../sdk/shared/docs/testing-generators.md)
