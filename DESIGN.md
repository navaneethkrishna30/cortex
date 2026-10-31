# Cortex UI design

The UI is a small admin tool for one person. It should feel quiet and be obvious to use.

## Where things live

- `src/cortex/static/tokens.css`: every colour, type size and layout size. The only CSS file with raw colours.
- `src/cortex/static/ui.css`: components, built only from tokens.
- Never: `style=` attributes, `<style>` blocks, or hex colours in templates or JS. The CSP (`style-src 'self'`) blocks inline styles, and `tests/test_ui_style.py` checks the source.
- `static/favicon.svg` is the one exception, since a favicon can't read CSS variables.

## Colour roles

| Role | Means | Use for |
|---|---|---|
| `--accent` (orange) | You can act here, or this is selected | Primary button, links, focus ring, current nav item and file |
| `--good` (teal) | Healthy or done | Success alerts, "Read and write" badge |
| `--warn` (yellow) | Take care | Warning alerts, "Unsaved" |
| `--bad` (red) | Failure or danger | Error alerts, "Failed" badge, danger buttons |
| Grays | Everything else | Text, surfaces, lines, folder icons |

Status colour goes on a badge or alert, never on bare text, so it can't be mistaken for a link.

## Type

`--text-xl` page title, `--text-lg` section and dialog title, `--text-md` body, `--text-sm` labels and secondary text, `--text-xs` badges. Mono is for paths, codes and commands.

## Layout

- The top bar is the same on every page, so nothing jumps between tabs.
- Page content sits in the centred `--content` column. Single forms use `--form`. Files alone runs edge to edge.
- Group with space first and hairlines second. Gaps between groups are at least twice the gaps inside them.
- Below 900px, Files shows the list or the note, never both. Below 720px, table rows become labelled cards; columns are never silently dropped.

## Components

- **Buttons:** one `primary` per view; `.btn` secondary; `danger` outline in lists; `danger solid` only to confirm in a dialog. Sizes: default (40px) and `sm` (32px). Stretch with `wide` inside cards; dialogs right-align their actions.
- **Alerts:** plain (info), `good`, `warn`, `bad`.
- **Copyable values:** always the `ui.command` macro.
- **Dialogs:** anything irreversible confirms and names what will happen on the button ("Revoke Claude"). Confirm when the action happens, not before.
- **Rare actions** go in a dialog or the More menu, not on the page.

## Words

Sentence case and plain sentences. Errors say what happened and what to do; no raw codes. Empty states say what to do next.
