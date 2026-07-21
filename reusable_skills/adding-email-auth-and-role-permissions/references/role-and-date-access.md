# Role And Date Access

## Separate Identity From Capability

Authentication establishes an identity. Authorization evaluates that identity against current server-side account state, requested resource, action, and product policy. Every protected list, detail, search, export, attachment, background job, and mutation endpoint must perform the same authoritative checks; hidden controls and client-provided claims are never an access boundary.

Define a capability matrix before implementation. A typical model is:

| Role | Read | Edit | Admin | Audit |
| --- | --- | --- | --- | --- |
| Viewer | Allowed only in visible date range | Denied | Denied | Denied |
| Editor | Allowed only in visible date range | Only explicitly listed fields in visible date range | Denied | Denied |
| Owner | Product-defined broad access | Product-defined | Manage permitted accounts and policies | Allowed when product requires it |

Roles name capabilities; they do not rewrite account history. Enforce status separately: a suspended or inactive account cannot read or mutate protected data, regardless of role.

## Registration-Date Visibility

When a product specifies registration-date access, store immutable `registered_at` and an `access_start_date` derived when email verification completes. Convert verification time to the product-specified business time zone, then compare calendar dates. A person verified at any time during that business day can see that entire day's records.

For a non-Owner account, a reusable predicate is:

```text
allowed_read = active AND (
  resource.business_date >= access_start_date
  OR explicit_history_access
)
```

Parenthesize this predicate in implementation so active status applies to both date paths. Owners may have product-defined broader access, but the policy must be explicit rather than an accidental consequence of a role label.

- Never change `registered_at` or `access_start_date` on later sign-in, password reset, role promotion, demotion, suspension, or restoration.
- Promotion from Viewer to Editor grants only the newly defined edit capability in the same visibility range. Promotion to Owner must not silently change the date boundary unless the product separately defines Owner-wide history.
- If an administrator needs historical review without changing ordinary account scope, model an independent, explicit, auditable history capability with its own grant and revoke path.
- Apply the predicate before pagination and aggregation so counts, search hits, exports, and error behavior do not leak excluded dates.

## Verification Checks

Test server-side allow and deny cases for every resource path, including same-day verification near a business-day boundary, earlier-day denial, later sign-in preservation, promotion without history expansion, explicit history grant and revoke, suspension, and client attempts to submit a different role or date.
