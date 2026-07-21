// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ReportItemDto } from '../../../../packages/shared/src/contracts';
import { DailyTable } from './DailyTable';
import { TranscriptPanel } from './TranscriptPanel';

function item(overrides: Partial<ReportItemDto> = {}): ReportItemDto {
  return {
    caption: '完整文稿内容',
    id: 'item-1',
    localRecordId: 'local-1',
    maxDailyCard: '口喷卡片',
    maxFeedback: '反馈',
    mediaId: 'media-1',
    reviewStatus: 'pending',
    sourceUrl: 'https://example.com/source',
    title: '素材标题',
    version: 3,
    ...overrides,
  };
}

describe('daily alternate views', () => {
  afterEach(cleanup);

  it('renders transcript content and source without a video player', () => {
    render(<TranscriptPanel item={item()} />);

    expect(screen.getByRole('heading', { name: '素材标题' })).toBeVisible();
    expect(screen.getByText('完整文稿内容')).toBeVisible();
    expect(screen.getByRole('link', { name: '打开原视频' })).toHaveAttribute('href', 'https://example.com/source');
    expect(document.querySelector('video')).toBeNull();
  });

  it('copies the complete transcript below the document', async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });

    render(<TranscriptPanel item={item({ caption: '完整\n文稿' })} />);
    await user.click(screen.getByRole('button', { name: '复制文稿' }));

    expect(writeText).toHaveBeenCalledWith('完整\n文稿');
    expect(await screen.findByRole('button', { name: '已复制' })).toBeVisible();
  });

  it('shows configured table fields and selects a material row', async () => {
    const onSelect = vi.fn();
    render(
      <DailyTable
        fields={['source', 'version', 'max_feedback']}
        items={[item(), item({ id: 'item-2', title: '第二条素材' })]}
        onSelect={onSelect}
        selectedItemId="item-1"
      />,
    );

    expect(screen.getByRole('columnheader', { name: '标题' })).toBeVisible();
    expect(screen.getByRole('columnheader', { name: '来源' })).toBeVisible();
    expect(screen.getByRole('columnheader', { name: '版本' })).toBeVisible();
    expect(screen.getByRole('columnheader', { name: 'MAX反馈' })).toBeVisible();
    expect(screen.queryByRole('columnheader', { name: '文稿' })).toBeNull();

    await userEvent.setup().click(screen.getByRole('button', { name: '选择第二条素材' }));
    expect(onSelect).toHaveBeenCalledWith('item-2');
  });

  it('renders a clear empty state when no materials match', () => {
    render(
      <DailyTable
        fields={['source']}
        items={[]}
        onSelect={() => undefined}
        selectedItemId={null}
      />,
    );

    expect(screen.getByText('没有符合条件的素材')).toBeVisible();
  });
});
