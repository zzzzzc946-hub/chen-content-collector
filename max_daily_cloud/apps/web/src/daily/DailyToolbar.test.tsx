// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DailyToolbar } from './DailyToolbar';

function renderToolbar() {
  const callbacks = {
    onFieldsChange: vi.fn(),
    onFilterQueryChange: vi.fn(),
    onLeftWidthChange: vi.fn(),
    onReviewStatusChange: vi.fn(),
    onRightWidthChange: vi.fn(),
    onRowDensityChange: vi.fn(),
    onSortDirectionChange: vi.fn(),
    onSortFieldChange: vi.fn(),
  };
  render(
    <DailyToolbar
      fields={['source', 'review_status', 'media', 'version']}
      filterQuery=""
      leftWidth={280}
      reviewStatus="all"
      rightWidth={360}
      rowDensity="standard"
      sortDirection="asc"
      sortField="report_order"
      {...callbacks}
    />,
  );
  return callbacks;
}

describe('DailyToolbar', () => {
  afterEach(cleanup);

  it('renders only workbench settings without alternate view tabs', () => {
    renderToolbar();

    expect(screen.queryByRole('tab')).toBeNull();
    expect(screen.getByRole('button', { name: '字段配置' })).toBeVisible();
    expect(screen.getByRole('button', { name: '筛选' })).toBeVisible();
    expect(screen.getByRole('button', { name: '排序' })).toBeVisible();
    expect(screen.getByRole('button', { name: '行高' })).toBeVisible();
    expect(screen.getByRole('button', { name: '调整空间' })).toBeVisible();
  });

  it('opens only one settings panel and updates field and filter controls', async () => {
    const callbacks = renderToolbar();
    const user = userEvent.setup();

    const fieldsButton = screen.getByRole('button', { name: '字段配置' });
    await user.click(fieldsButton);
    expect(fieldsButton).toHaveAttribute('aria-expanded', 'true');
    await user.click(screen.getByRole('checkbox', { name: '文稿' }));
    expect(callbacks.onFieldsChange).toHaveBeenCalledWith([
      'source', 'review_status', 'media', 'version', 'caption',
    ]);

    const filterButton = screen.getByRole('button', { name: '筛选' });
    await user.click(filterButton);
    expect(fieldsButton).toHaveAttribute('aria-expanded', 'false');
    expect(filterButton).toHaveAttribute('aria-expanded', 'true');
    await user.type(screen.getByRole('searchbox', { name: '筛选素材' }), '审美');
    expect(callbacks.onFilterQueryChange).toHaveBeenLastCalledWith('美');
  });

  it('wires sorting, row density, and space controls', async () => {
    const callbacks = renderToolbar();
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: '排序' }));
    await user.selectOptions(screen.getByLabelText('排序字段'), 'version');
    await user.click(screen.getByRole('button', { name: '降序' }));
    expect(callbacks.onSortFieldChange).toHaveBeenCalledWith('version');
    expect(callbacks.onSortDirectionChange).toHaveBeenCalledWith('desc');

    await user.click(screen.getByRole('button', { name: '行高' }));
    await user.click(screen.getByRole('button', { name: '紧凑' }));
    expect(callbacks.onRowDensityChange).toHaveBeenCalledWith('compact');

    await user.click(screen.getByRole('button', { name: '调整空间' }));
    const leftSlider = screen.getByLabelText('素材栏宽度');
    fireEvent.change(leftSlider, { target: { value: '320' } });
    expect(callbacks.onLeftWidthChange).toHaveBeenCalledWith(320);
    expect(screen.getByText('素材栏 280px')).toBeVisible();
    expect(screen.getByText('协同栏 360px')).toBeVisible();
  });
});
