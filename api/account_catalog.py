"""Static account services, credential instructions and endpoint defaults.

This leaf does not read saved accounts, acquire credential leases or contact
providers. Runtime authentication belongs to account_adapters.
"""
from __future__ import annotations

from collimate.providers import PROVIDERS, Provider

# A vendor plan's authentication method here: Anthropic's is the token
# `claude setup-token` prints; every other plan is a login a harness takes.
_PLAN_AUTH = {"anthropic": "subscription"}


def _model_service(provider: Provider) -> tuple:
    """A model provider as a service row: the SDK's provider registry is the
    one list of what a model entry can bill."""
    if not provider.api_key:
        protocol = "cli"
    else:
        protocol = provider.wire if provider.wire == provider.id else "openai-compatible"
    auth = (["api_key"] if provider.api_key else []) + (
        [_PLAN_AUTH.get(provider.id, "cli")] if provider.login_plan else [])
    return (provider.id, provider.label, protocol, provider.endpoint, auth, ("account", "model"), ("text",))


# (id, name, protocol, endpoint, auth types, kinds, capabilities). A git row's
# protocol is its service id, the dialect its Verify speaks; its endpoint is
# the web base its repositories open under (the API base is derived from it).
_SERVICES = (
    *(_model_service(provider) for provider in PROVIDERS.values()),
    ("github", "GitHub", "github", "https://github.com", ["api_key", "ssh_key"], ("git",), ("git",)),
    ("gitlab", "GitLab", "gitlab", "https://gitlab.com", ["api_key", "ssh_key"], ("git",), ("git",)),
    ("git", "Git server", "git", "", ["ssh_key", "basic", "none"], ("git",), ("git",)),
    ("tavily", "Tavily search", "tavily", "https://api.tavily.com", ["api_key"], ("account",), ("search",)),
    ("custom", "Custom service", "openai-compatible", "", ["api_key", "basic", "none"], ("custom", "account", "model"), ()),
)
# One instruction per offered (service, authentication) pair: what to paste
# and where it comes from. Shown under the Authentication method, and again
# under a failed Verify. Every credential is pasted; nothing signs in here —
# a plan whose CLI surface runs the vendor's CLI on this machine signs in
# inside that CLI's own terminal, and its paste is for the other surfaces.
_PLAN_DOCUMENT = ("Log in with the chosen app in a terminal on this machine, then paste the contents of the login file "
                  "it writes (the path is shown under the document field); Verify reads the document.")
_HELP = {
    "openai": {
        "api_key": "Create an API key at https://platform.openai.com/api-keys and paste it here.",
        "cli": f"ChatGPT plan: {_PLAN_DOCUMENT}",
    },
    "anthropic": {
        "api_key": "Create a key at https://console.anthropic.com/settings/keys and paste it here.",
        "subscription": ("Run `claude setup-token` in a terminal on this machine and paste the token it prints; "
                         "Verify checks it against the API. The Annotation and Chat surfaces need it. The CLI "
                         "surface runs this machine's claude and uses the token when there is one; without it, "
                         "sign in inside that terminal the first time (/login), once for this entry."),
    },
    "gemini": {
        "api_key": "Create a key at https://aistudio.google.com/app/apikey and paste it here.",
        "cli": ("Google plan: the CLI surface runs this machine's agy, which signs in through Google's own browser "
                "flow inside that terminal the first time and keeps the sign-in for this entry; nothing is pasted "
                "for it. For the Annotation and Chat surfaces, at your own risk (Google's terms restrict "
                "third-party use of consumer logins; a Gemini API key is the supported route): sign in once "
                "through Google's Antigravity ACP server in an editor Google documents (Zed or JetBrains; on macOS "
                "set AGY_ACP_FORCE_FILE_STORAGE=1 first), then paste the contents of "
                "~/.gemini/antigravity-acp/acp_token.json."),
    },
    "groq": {"api_key": "Create a key at https://console.groq.com/keys and paste it here."},
    "cerebras": {"api_key": "Create a key on the API keys page of the Cerebras cloud console and paste it here."},
    "openrouter": {"api_key": "Create a key at https://openrouter.ai/keys and paste it here."},
    "xai": {
        "api_key": "Create a key at https://console.x.ai and paste it here.",
        "cli": f"xAI plan: {_PLAN_DOCUMENT}",
    },
    "github": {
        "api_key": ("Create a fine-grained personal access token at https://github.com/settings/personal-access-tokens/new "
                    "for the repositories you will use, with Contents (read, or read and write to push) and Metadata "
                    "(read), and paste it here. A classic token with the repo scope works too, as does the token "
                    "`gh auth token` prints. For GitHub Enterprise, create it on that server and set Server URL to "
                    "the server's address."),
        "ssh_key": ("Paste an OpenSSH private key without a passphrase (the whole file, from -----BEGIN OPENSSH "
                    "PRIVATE KEY-----). Add its public key to your account (Settings › SSH and GPG keys) or to one "
                    "repository as a deploy key (with Allow write access to push); Verify shows the public key. A key "
                    "with a passphrase is refused: make an unencrypted copy with ssh-keygen -p -N \"\" -f <copy>, or "
                    "create a dedicated key with ssh-keygen -t ed25519 -N \"\". github.com's host keys are built in; "
                    "for GitHub Enterprise also paste the server's known_hosts line."),
    },
    "gitlab": {
        "api_key": ("Create a personal access token under Preferences › Access tokens on your GitLab "
                    "(https://gitlab.com/-/user_settings/personal_access_tokens on gitlab.com) with read_repository "
                    "(write_repository to push) and read_user, which Verify signs in with, and paste it here. A "
                    "project or group access token works for its own projects. For a self-managed instance, set "
                    "GitLab URL to its address first."),
        "ssh_key": ("Paste an OpenSSH private key without a passphrase. Add its public key under Preferences › SSH "
                    "keys, or to one project as a deploy key (with Grant write permissions to push); Verify shows the "
                    "public key. A key with a passphrase is refused: make an unencrypted copy with ssh-keygen -p -N "
                    "\"\" -f <copy>. gitlab.com's host keys are built in; for a self-managed instance paste the lines "
                    "its /help/instance_configuration page lists. Runs connect to git@<host> on port 22; for another "
                    "SSH port or user, use a Git server entry."),
    },
    "git": {
        "ssh_key": ("Paste an OpenSSH private key without a passphrase that the server accepts, and the server's "
                    "known_hosts lines: run ssh-keyscan -t ed25519 <host> (add -p <port> for another port) and compare "
                    "the fingerprint (ssh-keygen -lf -) with the one the server's administrator publishes. Collimate "
                    "never trusts a host on first use. Name repositories by their SSH address "
                    "(ssh://git@host:port/path or git@host:path)."),
        "basic": ("Enter the server's address as Server URL, and a user name with a token (or password) the server "
                  "accepts for git over HTTPS; they are sent, as HTTP Basic, only to repositories under that address. "
                  "Gitea and Forgejo: your user name and an access token. Bitbucket Cloud: x-token-auth and a "
                  "repository access token. Azure DevOps: any user name and a personal access token."),
        "none": "Public repositories over HTTPS: nothing is sent. Clone and update only; pushing needs a credential.",
    },
    "mistral": {"api_key": "Create a key at https://console.mistral.ai/api-keys and paste it here."},
    "copilot": {"cli": "GitHub Copilot plan: for the Copilot CLI, paste a fine-grained personal access token with the "
                       "Copilot Requests permission (https://github.com/settings/personal-access-tokens/new); for "
                       f"another app, {_PLAN_DOCUMENT[0].lower()}{_PLAN_DOCUMENT[1:]}"},
    "tavily": {"api_key": "Create a key at https://app.tavily.com and paste it here."},
    "custom": {
        "api_key": "Paste the key this service issues (sent as a bearer token); its own documentation says where to create one.",
        "basic": "Enter the username and password this service accepts for HTTP basic authentication.",
        "none": "This endpoint accepts anonymous requests, so no credentials are needed.",
    },
}
_GENERIC_HELP = "Check the service's documentation for where to obtain these credentials."
# What a service calls its credential where the generic name would mislead.
_AUTH_LABELS = {
    "github": {"api_key": "Personal access token", "ssh_key": "SSH key"},
    "gitlab": {"api_key": "Personal access token", "ssh_key": "SSH key"},
    "git": {"basic": "Username and token", "ssh_key": "SSH key", "none": "Public (no credentials)"},
}
_GENERIC_AUTH_LABELS = {"api_key": "API key", "basic": "Username and password"}


def auth_label(service: str, auth: str) -> str:
    """What the form calls an authentication method: the service's own name for it, else the generic one."""
    return _AUTH_LABELS.get(service, {}).get(auth) or _GENERIC_AUTH_LABELS.get(auth) or auth.replace("_", " ")


def service_catalog() -> list[dict]:
    return [{
        "id": identity, "name": name, "protocol": protocol,
        "protocols": [protocol], "endpoint": endpoint, "auth_types": auth,
        "kinds": list(kinds), "capabilities": list(capabilities),
        "help": {auth_type: auth_help(identity, auth_type) for auth_type in auth},
        "auth_labels": _AUTH_LABELS.get(identity, {}),
    } for identity, name, protocol, endpoint, auth, kinds, capabilities in _SERVICES]


def auth_help(service: str, auth_type: str) -> str:
    """Where this service's credential for ``auth_type`` comes from."""
    return _HELP.get(service, {}).get(auth_type, _GENERIC_HELP)


def endpoint(account: dict) -> str:
    configured = account.get("endpoint", "").rstrip("/")
    if configured:
        return configured
    return next((row[3] for row in _SERVICES if row[0] == account["service"]), "").rstrip("/")


