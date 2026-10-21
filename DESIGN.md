# CompassCairn — committed design direction

CompassCairn is a CLI, so its design system lives in the code (`compasscairn.py`) and in this
document rather than in CSS. These are the committed tokens; every rendered surface — the
`waypoint list` table, the `declination list` registry, the `drift list` report, the single-record
`waypoint add` block and the `--help` text — consumes them. Do not introduce a second palette,
a second accent, or a boxed/"card" style.

| Token | Value | Used for |
|---|---|---|
| Archetype | `cli` | Terminal product: no GUI, no TUI dashboard, no web view. |
| Style family | calm-precise | Plain sentences, exact numbers, no decoration. |
| Accent (one) | `oklch(0.58 0.12 155)` — hex `#318f5a` | The primary status only: the `FLAG` badge and the drift-report heading. |
| Accent (non-CSS / ANSI) | `\x1b[38;2;49;143;90m` (`#318f5a` true colour) | Same accent where oklch cannot be parsed (terminal). |
| Neutral ramp | terminal foregrounds: default text, `\x1b[2m` dim for secondary lines | Dark terminal surface only — the honest CLI surface. |
| Type personality | mono / mono | System monospace; columns depend on a fixed advance width. |
| Density | compact | Two-space gutters, one rule line, no blank-line padding between rows. |
| Shape language | monospace, no chrome | No boxes, no panels, no rounded frames; `-` rules under headers only; the same rule/separator system on *every* table. |
| Layout skeleton | aligned terminal columns, help-first output | `NAME / LOCATION / MAG / TRUE / DECL / CORR / DELTA / STATUS`; `--help` and the bare invocation both list commands. |
| Signal rule | never colour-only | Status is always a word: `FLAG` / `ok`. Colour is an extra, never the message. |

## Colour policy

* Colour is emitted **only** when stdout is a TTY.
* `NO_COLOR` in the environment or `--no-color` disables it unconditionally, so piped and
  recorded output is plain ASCII — safe for `diff`, `grep` and `jq`.
* There is exactly one accent. It never marks an error (errors are words on stderr), never
  marks success (success is the absence of `FLAG`), and never replaces text.

## Contrast

Terminal defaults are used for body text so the user's own light/dark theme keeps its contrast.
The accent `#318f5a` on a dark terminal background sits well above 4.5:1 at bold/regular weight,
and because it is never the only signal, a user who cannot distinguish it loses no information.

## Layout rules

1. Two spaces between columns; numeric columns right-aligned, text columns left-aligned.
2. One `-` rule directly under the header row of every table.
3. Single-record output (`waypoint add`) is a left-aligned `label  value` block, indented two spaces.
4. Timestamps are ISO-8601 UTC (`2026-10-21T20:59:41Z`) — sortable, diffable, unambiguous.
5. Every empty state is one line of *why* plus the exact command that fixes it.
