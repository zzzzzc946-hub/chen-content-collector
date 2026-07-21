# Acceptance Matrix

| Concern | Minimum evidence | Owning module |
| --- | --- | --- |
| Collection record | A source link creates a normalized local record with a clear operator status. | `collecting-cross-platform-content` |
| Daily snapshot | Selected local content produces a shareable snapshot without changing the local fact source. | `building-local-content-workbenches`, `publishing-collaborative-cloud-dailies` |
| Idempotent publish | Repeating the same approved publish request does not create a duplicate daily snapshot. | `publishing-collaborative-cloud-dailies` |
| Range video | Authorized video playback returns correct `Range` responses for seeking and resuming. | `publishing-collaborative-cloud-dailies` |
| Historical source link | A published item retains its original source link after later daily snapshots exist. | `collecting-cross-platform-content`, `publishing-collaborative-cloud-dailies` |
| Official release evidence | Release verification identifies the canonical source revision, installed artifact, runtime health, and rollback outcome when exercised. | `operating-native-desktop-products` |

Keep evidence environment-neutral. Do not place real credentials, private addresses, provider project identifiers, or production configuration values in this skill package.
