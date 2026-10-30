# Generators and named configs

The SDK ships the Agent cell's `agent` generator for model-powered work (its
Surface setting picks a document, a chat or a terminal), `git` for Git
repositories (GitHub, GitLab or any Git server) into and out of a cell, on
this machine, `launch` for
container commands and applications, and `web` for browser sessions and
capture. Each run reads and writes its own cell. The `_agent_shared`,
`_env_shared` and `_web_shared` folders hold their shared code;
`_agent_shared/session` is the Agent generator's runtime. `_env_shared`
holds `git_account.py` (the `git` account role, read once into a
`GitAccount`), `git_run.py` (git run with no ambient config, credential
helpers or hooks, and the HTTPS header or SSH key a remote gets) and
`repositories.py` (the clone, update and push engine of the `git`
generator; beside each cloned folder `<folder>/` it keeps
`<folder>.git-checkout.json`, the built-in `git-checkout` handler's type).

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
