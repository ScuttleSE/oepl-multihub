# Agent instructions

- Read `docs/PLAN.md` first. It is the full project writeup, plan and working notes. Keep its plan checkboxes,
  findings and open questions up to date.
- `README.md` is for users: what the project is, its status, and how to install and use it. Keep it short, and
  update it when something user-visible changes.
- Preferred language for host tools and the addon: Python, standard library where possible.
- C6 firmware work happens on a fork branch of upstream OpenEPaperLink. Don't vendor the whole upstream repo here.
- Tags must stay on stock OEPL firmware. Don't propose tag firmware changes.
- Home Assistant: read-only unless the user agrees to a change. Never print or commit secrets.
- The user prefers short answers and one step at a time.
- Commit and push after every change.
