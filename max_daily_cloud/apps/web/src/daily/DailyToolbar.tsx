import {
  AlignJustify,
  ArrowDownAZ,
  ArrowUpAZ,
  Columns3,
  ListFilter,
  Maximize2,
  Rows3,
  SlidersHorizontal,
} from 'lucide-react';
import { useState } from 'react';
import type {
  RowDensity,
  SortDirection,
  SortField,
  TableField,
} from './workbenchPreferences.js';

type SettingsPanel = 'fields' | 'filter' | 'sort' | 'density' | 'space';

interface DailyToolbarProps {
  fields: TableField[];
  filterQuery: string;
  leftWidth: number;
  onFieldsChange(fields: TableField[]): void;
  onFilterQueryChange(value: string): void;
  onLeftWidthChange(value: number): void;
  onReviewStatusChange(value: string): void;
  onRightWidthChange(value: number): void;
  onRowDensityChange(value: RowDensity): void;
  onSortDirectionChange(value: SortDirection): void;
  onSortFieldChange(value: SortField): void;
  reviewStatus: string;
  rightWidth: number;
  rowDensity: RowDensity;
  sortDirection: SortDirection;
  sortField: SortField;
}

const FIELD_OPTIONS: Array<{ label: string; value: TableField }> = [
  { label: '来源', value: 'source' },
  { label: '审核状态', value: 'review_status' },
  { label: '视频', value: 'media' },
  { label: '版本', value: 'version' },
  { label: '文稿', value: 'caption' },
  { label: 'MAX口喷卡片', value: 'max_daily_card' },
  { label: 'MAX反馈', value: 'max_feedback' },
];

const SETTINGS_OPTIONS: Array<{
  icon: typeof Columns3;
  label: string;
  value: SettingsPanel;
}> = [
  { icon: Columns3, label: '字段配置', value: 'fields' },
  { icon: ListFilter, label: '筛选', value: 'filter' },
  { icon: SlidersHorizontal, label: '排序', value: 'sort' },
  { icon: Rows3, label: '行高', value: 'density' },
  { icon: Maximize2, label: '调整空间', value: 'space' },
];

function toggleField(fields: TableField[], field: TableField): TableField[] {
  if (fields.includes(field)) return fields.filter((entry) => entry !== field);
  return [...fields, field];
}

export function DailyToolbar({
  fields,
  filterQuery,
  leftWidth,
  onFieldsChange,
  onFilterQueryChange,
  onLeftWidthChange,
  onReviewStatusChange,
  onRightWidthChange,
  onRowDensityChange,
  onSortDirectionChange,
  onSortFieldChange,
  reviewStatus,
  rightWidth,
  rowDensity,
  sortDirection,
  sortField,
}: DailyToolbarProps) {
  const [activePanel, setActivePanel] = useState<SettingsPanel | null>(null);

  return (
    <section className="toolbar-shell" aria-label="日报工具">
      <div className="workbench-toolbar">
        <div className="settings-tools" aria-label="显示设置">
          {SETTINGS_OPTIONS.map((option) => {
            const Icon = option.icon;
            const expanded = activePanel === option.value;
            return (
              <button
                aria-expanded={expanded}
                aria-haspopup="dialog"
                className={`toolbar-button toolbar-setting${expanded ? ' is-open' : ''}`}
                key={option.value}
                onClick={() => setActivePanel(expanded ? null : option.value)}
                type="button"
              >
                <Icon size={17} aria-hidden="true" />
                {option.label}
              </button>
            );
          })}
        </div>
      </div>

      {activePanel ? (
        <div className="toolbar-popover" role="dialog" aria-label={`${SETTINGS_OPTIONS.find((item) => item.value === activePanel)?.label}设置`}>
          {activePanel === 'fields' ? (
            <fieldset className="toolbar-fieldset">
              <legend>表格显示字段</legend>
              <div className="toolbar-check-grid">
                {FIELD_OPTIONS.map((option) => (
                  <label className="check-option" key={option.value}>
                    <input
                      checked={fields.includes(option.value)}
                      onChange={() => onFieldsChange(toggleField(fields, option.value))}
                      type="checkbox"
                    />
                    {option.label}
                  </label>
                ))}
              </div>
            </fieldset>
          ) : null}

          {activePanel === 'filter' ? (
            <div className="toolbar-control-grid two-columns">
              <label className="field compact-field">
                <span className="field-label">关键词</span>
                <input
                  aria-label="筛选素材"
                  className="field-input"
                  onChange={(event) => onFilterQueryChange(event.target.value)}
                  placeholder="搜索标题、来源或文稿"
                  type="search"
                  value={filterQuery}
                />
              </label>
              <label className="field compact-field">
                <span className="field-label">审核状态</span>
                <select
                  aria-label="筛选审核状态"
                  className="field-input"
                  onChange={(event) => onReviewStatusChange(event.target.value)}
                  value={reviewStatus}
                >
                  <option value="all">全部</option>
                  <option value="pending">pending</option>
                  <option value="approved">approved</option>
                  <option value="hold">hold</option>
                  <option value="rejected">rejected</option>
                  <option value="draft">draft</option>
                </select>
              </label>
            </div>
          ) : null}

          {activePanel === 'sort' ? (
            <div className="toolbar-control-grid two-columns">
              <label className="field compact-field">
                <span className="field-label">排序字段</span>
                <select
                  aria-label="排序字段"
                  className="field-input"
                  onChange={(event) => onSortFieldChange(event.target.value as SortField)}
                  value={sortField}
                >
                  <option value="report_order">日报原顺序</option>
                  <option value="title">标题</option>
                  <option value="review_status">审核状态</option>
                  <option value="version">版本</option>
                </select>
              </label>
              <div className="field compact-field">
                <span className="field-label">方向</span>
                <div className="segmented-control">
                  <button
                    aria-pressed={sortDirection === 'asc'}
                    className={sortDirection === 'asc' ? 'is-selected' : ''}
                    onClick={() => onSortDirectionChange('asc')}
                    type="button"
                  >
                    <ArrowUpAZ size={16} aria-hidden="true" />
                    升序
                  </button>
                  <button
                    aria-pressed={sortDirection === 'desc'}
                    className={sortDirection === 'desc' ? 'is-selected' : ''}
                    onClick={() => onSortDirectionChange('desc')}
                    type="button"
                  >
                    <ArrowDownAZ size={16} aria-hidden="true" />
                    降序
                  </button>
                </div>
              </div>
            </div>
          ) : null}

          {activePanel === 'density' ? (
            <div className="segmented-control density-control" aria-label="行高密度">
              {([
                ['compact', '紧凑'],
                ['standard', '标准'],
                ['comfortable', '宽松'],
              ] as Array<[RowDensity, string]>).map(([value, label]) => (
                <button
                  aria-pressed={rowDensity === value}
                  className={rowDensity === value ? 'is-selected' : ''}
                  key={value}
                  onClick={() => onRowDensityChange(value)}
                  type="button"
                >
                  <AlignJustify size={16} aria-hidden="true" />
                  {label}
                </button>
              ))}
            </div>
          ) : null}

          {activePanel === 'space' ? (
            <div className="toolbar-control-grid two-columns">
              <label className="range-control">
                <span>素材栏 {leftWidth}px</span>
                <input
                  aria-label="素材栏宽度"
                  max="420"
                  min="220"
                  onChange={(event) => onLeftWidthChange(Number(event.target.value))}
                  step="10"
                  type="range"
                  value={leftWidth}
                />
              </label>
              <label className="range-control">
                <span>协同栏 {rightWidth}px</span>
                <input
                  aria-label="协同栏宽度"
                  max="520"
                  min="300"
                  onChange={(event) => onRightWidthChange(Number(event.target.value))}
                  step="10"
                  type="range"
                  value={rightWidth}
                />
              </label>
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
