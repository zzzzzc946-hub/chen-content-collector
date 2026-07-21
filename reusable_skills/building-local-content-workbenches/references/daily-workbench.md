# Daily Workbench

## View, Not Replacement Fact Source

A daily workbench is a curation view over SQLite collection items. It stores selection, ordering, editorial edits, source-link attribution, and local preparation state without overwriting the original collection evidence.

## Operations

Create or select a daily, add or remove items, order them, edit daily-facing text, inspect the original source, and export a local representation. Store these actions as explicit daily and daily-item records so a restart preserves the view and auditability.

Downloads remain independent: a selected item can have no media, a pending download, a missing local file, or a ready local asset. The daily can still preserve text and its source link. A later publish workflow decides whether a particular media asset is needed for a publishable playback copy.

## Handoff

Publish handoff reads a stable daily snapshot from SQLite. It receives selected content, editorial fields, source links, and optional eligible media references. It must not mutate the collection fact source, delete local files, or treat a cloud copy as the authoritative media record.

## Validation

Verify that selecting, editing, and exporting one daily leaves the source collection record unchanged. Verify that removing or losing an optional download does not erase the daily item's text or source attribution.
