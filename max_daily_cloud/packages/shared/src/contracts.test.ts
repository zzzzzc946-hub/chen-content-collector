import { describe, expect, it } from 'vitest';
import { canEditField } from './contracts';

describe('canEditField', () => {
  it('allows editors to change only collaborative fields', () => {
    expect(canEditField('editor', 'max_daily_card')).toBe(true);
    expect(canEditField('editor', 'max_feedback')).toBe(true);
    expect(canEditField('editor', 'review_status')).toBe(true);
    expect(canEditField('editor', 'title')).toBe(false);
    expect(canEditField('viewer', 'max_daily_card')).toBe(false);
  });

  it('allows fixed-link collaborators to edit only collaborative fields', () => {
    expect(canEditField('collaborator', 'max_daily_card')).toBe(true);
    expect(canEditField('collaborator', 'max_feedback')).toBe(true);
    expect(canEditField('collaborator', 'review_status')).toBe(true);
    expect(canEditField('collaborator', 'title')).toBe(false);
    expect(canEditField('collaborator', 'caption')).toBe(false);
    expect(canEditField('collaborator', 'source_url')).toBe(false);
  });
});
