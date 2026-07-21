# Platform Adapters

## Adapter Contract

Each adapter accepts a normalized source link and optional session capability. It returns a common record shape: platform, canonical source link, title, text or caption, cover, duration, engagement, publication time, media candidates, extraction evidence, and collection outcome.

Keep platform selectors, URL rules, field names, and media limitations inside the adapter. The caller must not infer one platform's behavior from another platform's response.

## Selection

1. Canonicalize the input without discarding the original submitted link.
2. Match a dedicated adapter before trying generic page metadata.
3. Prefer public or documented metadata; use a real browser only for a page-state requirement.
4. Return an unsupported or manual-review outcome when identity or extraction is unsafe to infer.

## Common Variations

Reusable adapters should accommodate short-video, image-and-text, long-video, and hosted-video sources. A concrete product can support platforms such as Douyin, Xiaohongshu, Bilibili, WeChat Channels, YouTube, and Instagram without making those names a dependency of the module.

An image-and-text item is still a valid collection result even when it has no downloadable video. A media restriction, verification wall, or unavailable field must remain explicit evidence rather than an invented empty success.

## Handoff

Return caption availability separately from metadata success. Return browser need and authentication evidence separately from extraction errors so the caller can route browser recovery, transcription fallback, or operator status mapping correctly.
