# Agent Rules — Mandatory Standards

These rules are **non-negotiable**. Follow them on every task, in every language, across all parts of the codebase. Do not skip, shortcut, or defer them.

---

## 1. Logging & Observability

- **Always use a structured logger** — never use `console.log`, `print()`, or `fmt.Println()` for application logging. 
- Every log entry **must include**: `timestamp`, `level`, `service/module name`, and a `message`.
- Use the correct log level:
  - `debug` — internal state, variable dumps (dev only)
  - `info` — normal lifecycle events (start, stop, request received)
  - `warn` — recoverable issues, degraded state
  - `error` — failures that need attention; always include the error object/stack
- For request/response logging, log: method, path, status code, duration. Redact auth headers and sensitive query params.
- Add a correlation/trace ID to logs in any async or multi-service context.
- When adding a new feature or service, add an `info` log at startup and an `error` log on shutdown failure.
- Log extensively especially in pipelines.

---

## 2. Error Handling

- **Never swallow errors silently.** Every caught error must be logged or re-thrown with context.
- Do not use bare `except:` (Python) or empty `catch {}` (JS/TS) blocks — always catch a specific type or at minimum log the unknown error.
- Wrap external calls (network, DB, filesystem, third-party APIs) in try/catch and handle failure explicitly.
- Use typed/custom error classes for domain errors — do not throw raw strings.
- Distinguish between **operational errors** (expected, handle gracefully) and **programmer errors** (unexpected, crash loudly in dev, alert in prod).
- In async code, always handle promise rejections — no unhandled `.catch()` gaps.
- Surface meaningful error messages to callers; never expose raw stack traces or internal details to end users.
- In Python, use `raise ... from err` to preserve error chains.

---

## 3. Code Style & Structure

- Follow the existing conventions in the file you're editing — consistency beats personal preference.
- **Functions must do one thing.** If a function needs a comment explaining its second responsibility, split it.
- Maximum function length: ~40 lines. If longer, extract helpers.
- Name variables and functions for what they represent, not how they're implemented (`getUserById`, not `fetchData2`).
- TypeScript: always type function parameters and return values. No `any` unless explicitly justified with a comment.
- Python: use type hints on all public functions. Follow PEP 8.
- Keep imports ordered: stdlib → third-party → internal. Use an auto-formatter (Prettier, Black, gofmt) — do not fight it.

---

## 4. Security & Secrets

- **Never hardcode secrets, API keys, tokens, or credentials** in source code — not even in comments or tests.
- All secrets come from environment variables or a secrets manager. Access them via `process.env.SECRET_NAME`, `os.getenv("SECRET_NAME")`, etc.
- Never commit `.env` files. Commit only `.env.example` with placeholder values.
- Avoid logging anything that could appear in a secrets scan: tokens, session IDs, private keys.

---

## General

- If a rule conflicts with a specific project decision, **defer to the project** and note the deviation in a comment.
- If you are unsure whether a choice violates these rules, **ask before proceeding**.
- Propose improvements to these rules via PR, not by silently ignoring them.