// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ReportItemDto } from '../../../../packages/shared/src/contracts';
import { MaterialList } from './MaterialList';

const item: ReportItemDto = {
  caption: '文稿',
  id: 'item-1',
  localRecordId: 'local-1',
  maxDailyCard: '卡片',
  maxFeedback: '',
  mediaId: 'media-1',
  reviewStatus: 'pending',
  sourceUrl: 'https://example.com/item-1',
  title: '素材一',
  version: 1,
};

describe('MaterialList', () => {
  afterEach(cleanup);

  it('labels the fixed-link collaborator role as collaboration access', () => {
    render(
      <MaterialList
        items={[item]}
        onSelect={vi.fn()}
        role="collaborator"
        selectedItemId={item.id}
      />,
    );

    expect(screen.getByText('协作者 Collaborator')).toBeVisible();
    expect(screen.queryByText('Public')).toBeNull();
  });

  it('marks the material list as a carousel without changing selection semantics', () => {
    render(
      <MaterialList
        items={[item]}
        onSelect={vi.fn()}
        role="viewer"
        selectedItemId={item.id}
      />,
    );

    expect(screen.getByRole('list')).toHaveClass('timeline-list', 'material-carousel');
    expect(screen.getByRole('button', { name: /素材一/ })).toHaveAttribute('aria-pressed', 'true');
  });
});
