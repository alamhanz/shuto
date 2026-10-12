# Security policy

## Reporting a vulnerability

Please report security problems privately through GitHub: open the repository's **Security**
tab and choose **Report a vulnerability**. Do not open a public issue with exploit details.
You can expect an acknowledgement within about a week.

## Scope and design notes

- The web UI binds to `127.0.0.1` only and checks Host and Origin headers and a CSRF token. Do not expose it to a network.
- Secrets (FTPS login, API keys) come from environment variables, `.env` or the OS keyring. They are never stored in the config or shown in the UI.
- Network traffic goes only to the configured LLM endpoint (default: local Ollama) and the FTPS host. With an API provider, images are sent to that vendor.
- Originals are read in place and never modified; prepared copies live in the workspace.
- GPS data in photos is not stripped in v1.

## Supported versions

Only the latest release on `main` receives fixes.
