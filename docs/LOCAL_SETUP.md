# Local development setup (macOS)

## Requirements
- **Python 3.11.** Newer versions (e.g. 3.14) fail to install the pinned Pillow 10.0.1.
- **unixODBC**, required by `pyodbc`.

## Setup
```bash
brew install python@3.11 unixodbc
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

## Configuration
- The app reads settings from environment variables. It does not load a `.env` file.
- SQL Server settings are defined in `py/utilities/setting.py`. Without credentials,
  pages that query SQL Server will error, but the app and non-database features still run.
- Never point a local setup at production credentials.

## Debug mode
When started by hand, debug mode is on by default. When running as a Windows service
(or with `--no-reloader`), it is forced off. See the `__main__` block in `app.py`.

## Tests
`requirements-dev.txt` adds test-only packages (pytest, reportlab).

Known issue: pytest cannot import modules from the `py/` folder, because pytest ships
its own internal module named `py`. Fixing this requires renaming the folder.
