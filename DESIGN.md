# Cortex UI design

The UI is a small tool for one person, and it should be loud and obvious: thick lines, hard shadows, big type, and one colour per vault folder.

## Where things live

- `src/cortex/static/tokens.css`: every colour, type size and layout size. The only CSS file with raw colours.
- `src/cortex/static/ui.css`: components, built only from tokens.
- Never: `style=` attributes, `<style>` blocks, or hex colours in templates or JS. The CSP (`style-src 'self'`) blocks inline styles, and `tests/test_ui_style.py` checks the source.
- `static/favicon.svg` is the one exception, since a favicon can't read CSS variables.

## Colour roles

| Role | Means | Use for |
|---|---|---|
| `--accent` (pink) | The main thing to do here | Primary button, focus on inputs |
| `--good` (lime) | Healthy or done | Success alerts, new-token panel |
| `--warn` (yellow) | Take care | Warning alerts |
| `--bad` (pink) | Something failed | Error alerts, failed activity |
| `--danger` (red) | Destroys something | Confirm button in a dialog, Archive |
| `--link-mark` (cyan) | A link between notes | Wikilinks, "Not saved" |
| `--folder` | The folder this belongs to | Tabs, explorer cards, a note's page |

Folders: core pink, memory yellow, entities cyan, docs lime, logs lilac. The page ground is cobalt, and a note's page takes its folder's colour.

Everything sits on a white sheet with black ink. Text on a colour is always black, so contrast never depends on the colour.

## Type

Bricolage Grotesque for everything people read, with weight 800 for headings. DM Mono for paths, codes and the editor. Both are self-hosted, because the CSP blocks font hosts. Sizes are the `--text-*` tokens.

## Layout

- The top bar is the same on every page. Authenticator is a quiet link beside Log out, because it's rare.
- Page content sits on a sheet in the centred `--wrap` column. Single forms use `--form`.
- Folder tabs stick out of the top of the Access sheet.
- Small tilts are for stickers: client cards, failed events, the brand. Never tilt forms or long text.
- Below 900px, Files shows the list or the note, never both.

## Notes

A note opens as a page rendered by the server (`note_view.py`), with raw HTML escaped. Edit shows the source in a plain textarea, and Read always shows the last save. An editor library isn't worth its weight here.

## Components

- **Buttons:** one `primary` per view; `.btn` secondary; `plain` for repeated list actions like Revoke; `danger solid` only to confirm in a dialog. Sizes: default (44px), `sm` (40px) and `big` for the one action a page exists for. Stretch with `wide` inside cards; dialogs right-align their actions.
- **Alerts:** plain (info), `good`, `warn`, `bad`.
- **Copyable values:** always the `ui.command` macro.
- **Dialogs:** anything irreversible confirms and names what will happen on the button ("Revoke Claude"). Confirm when the action happens, not before.
- **Rare actions** go in a dialog or the More menu, not on the page.

## Words

Sentence case and plain sentences. Errors say what happened and what to do; no raw codes. Empty states say what to do next.
