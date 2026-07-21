import type { ReportItemDto } from '../../../../packages/shared/src/contracts.js';

export type RowDensity = 'compact' | 'standard' | 'comfortable';
export type SortField = 'report_order' | 'title' | 'review_status' | 'version';
export type SortDirection = 'asc' | 'desc';
export type TableField =
  | 'source'
  | 'review_status'
  | 'media'
  | 'version'
  | 'caption'
  | 'max_daily_card'
  | 'max_feedback';

export interface WorkbenchPreferences {
  fields: TableField[];
  leftWidth: number;
  rightWidth: number;
  rowDensity: RowDensity;
  sortDirection: SortDirection;
  sortField: SortField;
}

export interface WorkbenchFilterOptions {
  query: string;
  reviewStatus: string;
  sortDirection: SortDirection;
  sortField: SortField;
}

interface PreferenceStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

const STORAGE_KEY = 'max-daily-workbench-preferences:v1';
const ROW_DENSITIES = new Set<RowDensity>(['compact', 'standard', 'comfortable']);
const SORT_FIELDS = new Set<SortField>(['report_order', 'title', 'review_status', 'version']);
const SORT_DIRECTIONS = new Set<SortDirection>(['asc', 'desc']);
const TABLE_FIELDS = new Set<TableField>([
  'source',
  'review_status',
  'media',
  'version',
  'caption',
  'max_daily_card',
  'max_feedback',
]);

export const DEFAULT_WORKBENCH_PREFERENCES: WorkbenchPreferences = {
  fields: ['source', 'review_status', 'media', 'version'],
  leftWidth: 280,
  rightWidth: 360,
  rowDensity: 'standard',
  sortDirection: 'asc',
  sortField: 'report_order',
};

function clamp(value: unknown, minimum: number, maximum: number, fallback: number): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) return fallback;
  return Math.min(maximum, Math.max(minimum, Math.round(value)));
}

function copyDefaults(): WorkbenchPreferences {
  return {
    ...DEFAULT_WORKBENCH_PREFERENCES,
    fields: [...DEFAULT_WORKBENCH_PREFERENCES.fields],
  };
}

export function loadWorkbenchPreferences(storage?: PreferenceStorage | null): WorkbenchPreferences {
  if (!storage) return copyDefaults();
  try {
    const value = storage.getItem(STORAGE_KEY);
    if (!value) return copyDefaults();
    const candidate = JSON.parse(value) as Partial<WorkbenchPreferences>;
    const fields = Array.isArray(candidate.fields)
      ? candidate.fields.filter((field): field is TableField => TABLE_FIELDS.has(field as TableField))
      : [...DEFAULT_WORKBENCH_PREFERENCES.fields];
    return {
      fields,
      leftWidth: clamp(candidate.leftWidth, 220, 420, DEFAULT_WORKBENCH_PREFERENCES.leftWidth),
      rightWidth: clamp(candidate.rightWidth, 300, 520, DEFAULT_WORKBENCH_PREFERENCES.rightWidth),
      rowDensity: ROW_DENSITIES.has(candidate.rowDensity as RowDensity)
        ? candidate.rowDensity as RowDensity
        : DEFAULT_WORKBENCH_PREFERENCES.rowDensity,
      sortDirection: SORT_DIRECTIONS.has(candidate.sortDirection as SortDirection)
        ? candidate.sortDirection as SortDirection
        : DEFAULT_WORKBENCH_PREFERENCES.sortDirection,
      sortField: SORT_FIELDS.has(candidate.sortField as SortField)
        ? candidate.sortField as SortField
        : DEFAULT_WORKBENCH_PREFERENCES.sortField,
    };
  } catch {
    return copyDefaults();
  }
}

export function saveWorkbenchPreferences(
  storage: PreferenceStorage | null | undefined,
  preferences: WorkbenchPreferences,
): void {
  if (!storage) return;
  try {
    storage.setItem(STORAGE_KEY, JSON.stringify(preferences));
  } catch {
    // UI preferences must never prevent the report from opening.
  }
}

export function filterAndSortItems(
  items: ReportItemDto[],
  options: WorkbenchFilterOptions,
): ReportItemDto[] {
  const query = options.query.trim().toLocaleLowerCase('zh-CN');
  const filtered = items.filter((item) => {
    if (options.reviewStatus !== 'all' && item.reviewStatus !== options.reviewStatus) return false;
    if (!query) return true;
    return [item.title, item.sourceUrl, item.caption]
      .some((value) => value.toLocaleLowerCase('zh-CN').includes(query));
  });
  const originalOrder = new Map(items.map((item, index) => [item.id, index]));
  const direction = options.sortDirection === 'desc' ? -1 : 1;

  return [...filtered].sort((left, right) => {
    let comparison = 0;
    if (options.sortField === 'title') comparison = left.title.localeCompare(right.title, 'zh-CN');
    if (options.sortField === 'review_status') {
      comparison = left.reviewStatus.localeCompare(right.reviewStatus, 'en');
    }
    if (options.sortField === 'version') comparison = left.version - right.version;
    if (options.sortField === 'report_order') {
      comparison = (originalOrder.get(left.id) ?? 0) - (originalOrder.get(right.id) ?? 0);
    }
    return comparison * direction;
  });
}
