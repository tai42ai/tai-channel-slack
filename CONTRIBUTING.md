# Contributing to tai42-channel-slack

`tai42-channel-slack` is a Slack **channel** plugin for the TAI ecosystem:
`ask_user(..., channel="slack")` posts the question to a configured Slack channel
via `chat.postMessage`; the human replies in the message's thread; the Slack
Events API delivers the reply to this plugin's inbound door, which verifies the
request signature and forwards the answer to the interaction's public callback
URL. The hard rule (the plugin rule): **it depends on `tai42-contract` + `tai42-kit`
only and never imports the skeleton** — the whole Slack surface is two HTTPS POSTs
plus stdlib `hmac` (no `slack_sdk`, no Bolt). The skeleton loads it through the
manifest's `channel_modules` field; `tai42_channel_slack.register` registers the
`"slack"` channel and the inbound route as a side-effect — there is no import
edge to the skeleton in either direction.

## Ground rules

- **No skeleton import — ever.** The package is contract-facing; the ban is
  enforced by ruff (`flake8-tidy-imports`), so a stray import fails lint:
  ```bash
  grep -rn "tai42_skeleton" src/   # must be empty
  ```
- **Credentials are operator-bound, never LLM-visible.** The signing secret and
  bot token come from the environment, never from a tool parameter; a requested
  recipient is honored only if it is on the operator allowlist, and an unlisted
  recipient fails loudly with nothing sent.
- **Fail closed.** An unset or empty signing secret makes the inbound door raise
  rather than ever verifying against a forgeable key; a delivery against an
  unconfigured channel raises, naming the missing env var.
- **Typed package** (`py.typed`). Pyright runs clean.

## Layout

- `register.py` — registers the `"slack"` channel and the inbound route as an
  import side-effect (a bare `import` registers nothing).
- `channel.py` — the outbound `Channel` implementation (`chat.postMessage`).
- `inbound.py` — the signed Events-API inbound door that bridges the threaded
  reply back to the callback.
- `client.py`, `correlation.py`, `settings.py` — the HTTP client, the Redis
  correlation store, and the `CHANNEL_SLACK_` settings.

## Naming

PyPI is a flat namespace with no owner in the path, so distributions carry the
`tai42-` prefix. GitHub repositories keep their `tai-` names, because the
`tai42ai` organisation already namespaces them. Import packages follow the
distribution.

| Surface | Form |
| --- | --- |
| Distribution — PyPI, `pip install`, dependency pins | `tai42-<name>` |
| Import package | `tai42_<name>` |
| GitHub repository | `tai-<name>` |

So a dependency is declared as `tai42-<name>` while its repository is named
`tai-<name>`, and both spellings are correct in their own context.

Some surfaces are deliberately neither, and must not be renamed: the `tai` CLI
command (`tai42` is an alias), the Prometheus metric namespace (`tai_tool_*`),
`TAI_*` environment variables, and the `tai-plugin.yml` descriptor filename.

## Dev

```bash
uv venv --python 3.13
uv pip install --no-sources --group dev --editable .
uv run --no-sync pytest --cov --cov-report=term-missing
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pyright
```

`make dev` installs the sibling `tai-contract` and `tai-kit` repos as editable installs for local cross-repo development.

Before any commit, run a secret scan over `src/` and `tests/` (e.g.
`detect-secrets scan`).

## License

By contributing you agree your contributions are licensed under Apache-2.0.
