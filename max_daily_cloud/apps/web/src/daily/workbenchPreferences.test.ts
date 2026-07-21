import { describe, expect, it } from 'vitest';
import type { ReportItemDto } from '../../../../packages/shared/src/contracts';
import {
  DEFAULT_WORKBENCH_PREFERENCES,
  filterAndSortItems,
  loadWorkbenchPreferences,
  saveWorkbenchPreferences,
} from './workbenchPreferences';

function item(overrides: Partial<ReportItemDto>): ReportItemDto {
  return {
    caption: '',
    id: 'item-1',
    localRecordId: 'local-1',
    maxDailyCard: '',
    maxFeedback: '',
    mediaId: null,
    reviewStatus: 'pending',
    sourceUrl: '',
    title: '素材',
    version: 1,
    ...overrides,
  };
}

describe('workbench preferences', () => {
  it('falls back to safe defaults when persisted data is malformed', () => {
    const storage = {
      getItem: () => '{broken',
      setItem: () => undefined,
    };

    expect(loadWorkbenchPreferences(storage)).toEqual(DEFAULT_WORKBENCH_PREFERENCES);
  });

  it('sanitizes persisted values and never keeps unknown fields', () => {
    const storage = {
      getItem: () => JSON.stringify({
        fields: ['source', 'secret', 'caption'],
        leftWidth: 9999,
        rightWidth: 20,
        rowDensity: 'tiny',
        sortDirection: 'desc',
        sortField: 'version',
        viewMode: 'table',
      }),
      setItem: () => undefined,
    };

    expect(loadWorkbenchPreferences(storage)).toEqual({
      fields: ['source', 'caption'],
      leftWidth: 420,
      rightWidth: 300,
      rowDensity: 'standard',
      sortDirection: 'desc',
      sortField: 'version',
    });
  });

  it('saves only the supplied interface preferences', () => {
    let saved = '';
    const storage = {
      getItem: () => null,
      setItem: (_key: string, value: string) => {
        saved = value;
      },
    };

    saveWorkbenchPreferences(storage, DEFAULT_WORKBENCH_PREFERENCES);

    expect(JSON.parse(saved)).toEqual(DEFAULT_WORKBENCH_PREFERENCES);
    expect(saved).not.toContain('token');
  });

  it('filters title, source, caption, and review status before sorting', () => {
    const items = [
      item({ id: 'a', title: '机场朋友圈', sourceUrl: 'https://douyin.com/a', version: 2 }),
      item({ caption: '审美提升方法', id: 'b', reviewStatus: 'approved', title: '排版', version: 5 }),
      item({ id: 'c', sourceUrl: 'https://xiaohongshu.com/c', title: '留白', version: 3 }),
    ];

    expect(filterAndSortItems(items, {
      query: '审美',
      reviewStatus: 'approved',
      sortDirection: 'asc',
      sortField: 'report_order',
    }).map((entry) => entry.id)).toEqual(['b']);

    expect(filterAndSortItems(items, {
      query: 'douyin',
      reviewStatus: 'all',
      sortDirection: 'asc',
      sortField: 'report_order',
    }).map((entry) => entry.id)).toEqual(['a']);
  });

  it('sorts title and version in both directions without mutating input', () => {
    const items = [
      item({ id: 'a', title: 'C', version: 2 }),
      item({ id: 'b', title: 'A', version: 5 }),
      item({ id: 'c', title: 'B', version: 3 }),
    ];

    expect(filterAndSortItems(items, {
      query: '', reviewStatus: 'all', sortDirection: 'asc', sortField: 'title',
    }).map((entry) => entry.id)).toEqual(['b', 'c', 'a']);
    expect(filterAndSortItems(items, {
      query: '', reviewStatus: 'all', sortDirection: 'desc', sortField: 'version',
    }).map((entry) => entry.id)).toEqual(['b', 'c', 'a']);
    expect(items.map((entry) => entry.id)).toEqual(['a', 'b', 'c']);
  });
});
