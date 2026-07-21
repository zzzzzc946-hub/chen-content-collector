begin;

lock table
  public.reports,
  public.report_items,
  public.report_publication_items
in share row exclusive mode;

update public.report_publication_items rpi
set
  max_daily_card = ri.max_daily_card,
  max_feedback = ri.max_feedback,
  review_status = ri.review_status,
  item_version = ri.version
from public.reports r
join public.report_items ri on ri.report_id = r.id
where r.status = 'published'
  and r.published_version = rpi.published_version
  and r.id = rpi.report_id
  and ri.id = rpi.report_item_id
  and (
    rpi.max_daily_card is distinct from ri.max_daily_card
    or rpi.max_feedback is distinct from ri.max_feedback
    or rpi.review_status is distinct from ri.review_status
    or rpi.item_version is distinct from ri.version
  );

commit;
