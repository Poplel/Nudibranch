# UI Direction

Nudibranch uses a file-manager style interface with a persistent sidebar,
top-left notification tray, tree-first library view, and right-side inspector.

## Required Interaction Patterns

- approvals are bulk-first, with checkboxes for granular deselection
- no per-item pop-up prompts for normal proposal review
- drag-and-drop moves become proposals, not direct mutations
- import wizard reviews `/app/import` before files move anywhere permanent
- notifications open the relevant approval queue or task detail

## Pages

```txt
Library
Import
Wishlist
Approvals
Downloads
Playlists
Tasks
Users
Settings
Jellyfin
API Docs
```

## Theme

- light by default
- dark toggle
- restrained animation
- compact controls
- no cover-grid primary browsing mode

