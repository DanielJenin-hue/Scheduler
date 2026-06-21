# Cursor SDK (optional)

Programmatic FINISH_APP ticks via the [Cursor Python SDK](https://cursor.com/docs/sdk/python). The repo does not require `cursor-sdk` for pytest or the Streamlit app.

## Install

```bash
pip install cursor-sdk
```

Or from the repo root:

```bash
pip install -e ".[sdk]"
```

## API key

Create a user API key at [Cursor Dashboard → Integrations](https://cursor.com/dashboard/integrations). Do not commit keys to the repo.

```powershell
$env:CURSOR_API_KEY = "cursor_..."
```

## Run a tick

From the repo root (local runtime — agent uses this checkout as `cwd`):

```powershell
python scripts/sdk_first_dollar_tick.py
```

Dry-run (prints the prompt, no API call):

```powershell
python scripts/sdk_first_dollar_tick.py --dry-run
```

Exit codes: `0` finished, `1` startup failure (`CursorAgentError` or missing deps/key), `2` run started but failed (`result.status == "error"`).

## Local vs cloud

This project uses **local** agents (`LocalAgentOptions(cwd=repo_root)`) so ticks reuse your machine, venv, and git state. Cloud agents (`CloudAgentOptions`) clone the repo on Cursor infrastructure — useful for long jobs or PR automation, but not the default here.
