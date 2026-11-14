# Agent instructions: Claude Critter

Claude Critter is a Claude Code plugin plus a Python desktop overlay for the Claude desktop app on Windows.

## Install (Windows, PowerShell)

1. Make sure Python 3.10+ is installed and `pythonw` is on PATH.
2. `python -m pip install --user comtypes`
3. `claude plugin marketplace add HannesKapsch/claude-critter`
4. `claude plugin install critter@claude-critter`
5. Tell the user to restart the Claude desktop app.

Or run the installer: `irm https://raw.githubusercontent.com/HannesKapsch/claude-critter/main/install.ps1 | iex`

## Use

- `/critter` starts the overlay if it is not running (only one instance ever runs).
- The plugin needs no configuration and uses no model calls.

## Files

- `.claude-plugin/plugin.json`: plugin manifest. `.claude-plugin/marketplace.json`: makes the repo installable as a marketplace.
- `hooks/register.tsx`: hooks that write `~/.claude/critter/state.json` and start the overlay.
- `overlay/critter.pyw`: the overlay (tkinter + comtypes UI Automation). Logs errors to `~/.claude/critter/overlay.err`.
