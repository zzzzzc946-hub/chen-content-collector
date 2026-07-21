# Current Day And Retention

## Time Policy

When a product specifies a business time zone and access window, use them exactly. Derive `business_date` from that named zone and compare dates, rather than comparing browser-local dates or ambiguous server timestamps. Do not replace a specified calendar-day policy with a timestamp rule, or substitute a generic retention duration.

Authorize playback only when the report business date equals the current business date. The report can remain visible before or after that window, but historical items render metadata and their permanent original source link without a playable media control or media identifier.

## Retention Policy

Use the product's fixed retention duration, such as 72 hours, from the completed-upload timestamp. Do not replace it with a generic 180- or 365-day policy. Use a single stored instant for `upload_completed_at`; do not reset it on retry, view, metadata edit, or report republish. The cleanup worker deletes only the associated cloud playback copy after the cutoff even when historical report or manifest metadata still refers to it, and records success or retryable failure.

| State | Report content | Playback copy |
| --- | --- | --- |
| Current business day and retained | Visible | May be authorized. |
| Historical but retained | Visible with permanent source link | Hidden from the workbench and not served. |
| Expired or deleted | Visible with permanent source link | Absent; cleanup may retry safely. |

## Evidence

Test both sides of midnight in the specified zone, a different browser time zone, a retry after upload completion, and an expired-copy cleanup with a remaining historical metadata reference. Verify that the report and original source link survive every media state and that no local file is selected for deletion.
