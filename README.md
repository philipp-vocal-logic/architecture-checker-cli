# archcheck

Minimal, zero-noise architecture checker CLI for agentic coding tools (Python 3.12+, zero runtime dependencies).

## Configuration (`arch.toml`)

```toml
[[rules]]
files = ["src/domain/**/*.py"]
ignore = ["**/*test*.py"]
max_lines = 300
forbid = [
    "^\\s*(from|import)\\s+.*\\b(app|ui|infra|sqlalchemy)\\b",
    "^\\s*from\\s+\\.{2,}",
    "\\bos\\.(environ|getenv)\\b",
]
fix = "Import direction is ui -> app -> domain. Keep domain pure and use ports."
```

### Rule Keys
- `files`: Glob patterns to check (required). When both `max_lines` and `forbid` are omitted, any matched file not excluded by `ignore` is reported as `<path>: forbidden file`.
- `fix`: Actionable instruction printed once per broken rule (required).
- `ignore`: Glob patterns to exclude (optional).
- `max_lines`: Maximum lines per file (optional).
- `forbid`: Regular expressions forbidden on any line (optional).

## Usage

```bash
# Check entire configured workspace
uv run architecture

# Filter to specific file(s) or directory(s)
uv run architecture src/archcheck.py
uv run architecture tests/

# Override config file path
uv run architecture --config path/to/arch.toml
```

### Output Contract
- **Pass (exit `0`):** `0` bytes output.
- **Fail (exit `1`, `stderr`):**
  ```text
  src/domain/order.py: 340/300 lines
  src/domain/order.py:4: from app.infra.db import Session
  fix: Import direction is ui -> app -> domain. Keep domain pure and use ports.
  ```
- **Error (exit `2`, `stderr`):** Invalid config, regex, encoding, or missing target path.

## Development

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run architecture
uv run glossary
uv run symbol
```
