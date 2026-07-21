// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import type { ReportIndexEntryDto } from '../../../../packages/shared/src/contracts';
import { ReportDateNav } from './ReportDateNav';

const entries: ReportIndexEntryDto[] = [
  {
    dailyDate: '2026-07-15',
    id: 'report-new',
    itemCount: 3,
    publishedAt: '2026-07-15T12:00:00.000Z',
  },
  {
    dailyDate: '2026-07-14',
    id: 'report-old',
    itemCount: 1,
    publishedAt: '2026-07-14T12:00:00.000Z',
  },
];

afterEach(() => {
  cleanup();
});

it('renders semantic date buttons with a clear selected state', async () => {
  const onSelect = vi.fn();
  const user = userEvent.setup();

  render(
    <ReportDateNav
      entries={entries}
      loadingReportId={null}
      onSelect={onSelect}
      selectedReportId="report-new"
    />,
  );

  const newest = screen.getByRole('button', { name: /2026-07-15/ });
  const older = screen.getByRole('button', { name: /2026-07-14/ });

  expect(screen.getByRole('navigation', { name: '日报日期' })).toBeVisible();
  expect(newest).toHaveAttribute('aria-current', 'date');
  expect(newest).toHaveAttribute('aria-pressed', 'true');
  expect(older).toHaveAttribute('aria-pressed', 'false');

  await user.tab();
  expect(newest).toHaveFocus();
  await user.keyboard('{ArrowRight}');
  expect(older).toHaveFocus();
  await user.keyboard('{Enter}');

  expect(onSelect).toHaveBeenCalledWith('report-old');
});

it('shows stable loading, empty, and error states', () => {
  const { rerender } = render(
    <ReportDateNav
      entries={entries}
      loadingReportId="report-old"
      onSelect={vi.fn()}
      selectedReportId="report-new"
    />,
  );

  expect(screen.getByRole('button', { name: /2026-07-14/ })).toHaveAttribute('aria-busy', 'true');
  expect(screen.getByText('正在切换')).toBeVisible();

  rerender(
    <ReportDateNav
      entries={[]}
      loadingReportId={null}
      onSelect={vi.fn()}
      selectedReportId={null}
    />,
  );
  expect(screen.getByText('还没有已发布日报')).toBeVisible();

  rerender(
    <ReportDateNav
      entries={entries}
      errorMessage="协作链接已失效，请重新打开原始固定链接"
      loadingReportId={null}
      onSelect={vi.fn()}
      selectedReportId="report-new"
    />,
  );
  expect(screen.getByText('协作链接已失效，请重新打开原始固定链接')).toBeVisible();
});
