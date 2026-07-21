# Module Map

| Module | Inputs | Outputs | Depends on |
| --- | --- | --- | --- |
| `collecting-cross-platform-content` | Public content links, operator intent, available browser session | Normalized collection records, media metadata, transcription state, explicit operator status | Platform access and optional transcription provider |
| `building-local-content-workbenches` | Collection records, selected media metadata, local operator actions | SQLite-backed selections, download state, daily workbench view, locally playable media | Collection module output |
| `publishing-collaborative-cloud-dailies` | Selected local daily content, approved publish request, removable media copies | Immutable daily snapshot, collaboration view, authorized media delivery | Local workbench output |
| `operating-native-desktop-products` | Canonical source state, release candidate, runtime checks | Native-hosted service, release evidence, rollback result when needed | A verified product build and canonical source policy |
| `adding-email-auth-and-role-permissions` | Registration intent, authenticated session, role and access policy | Session state, role decision, date-bound access decision, audit evidence | Cloud collaboration boundary |

Choose the module that owns the requested state transition. A downstream module may consume an upstream output, but it must not rewrite the upstream module's fact source.
