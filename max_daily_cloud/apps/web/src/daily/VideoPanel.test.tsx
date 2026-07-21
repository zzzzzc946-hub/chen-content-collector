// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import type { ReportItemDto } from '../../../../packages/shared/src/contracts';
import { VideoPanel } from './VideoPanel';

const item: ReportItemDto = {
  caption: 'source caption',
  id: 'item-1',
  localRecordId: 'local-1',
  maxDailyCard: 'card',
  maxFeedback: '',
  mediaId: 'media-1',
  reviewStatus: 'pending',
  sourceUrl: 'https://example.com/source',
  title: 'source title',
  version: 1,
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it('renders a current-day player after bootstrapping a media session', async () => {
  const fetcher = vi.fn(async () => new Response(null, { status: 204 }));
  const createObjectUrl = vi.fn();
  vi.stubGlobal('fetch', fetcher);
  vi.stubGlobal('URL', { createObjectURL: createObjectUrl });

  const { container } = render(
    <VideoPanel
      accessToken="editor-token"
      apiBaseUrl="https://worker.example.com"
      item={item}
      mediaMode="current_day"
      role="editor"
      sessionMode="identity"
    />,
  );

  await waitFor(() => {
    expect(container.querySelector('video')).not.toBeNull();
  });
  const video = container.querySelector('video');

  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(fetcher).toHaveBeenCalledWith(
    'https://worker.example.com/api/media/session',
    expect.objectContaining({
      credentials: 'include',
      headers: { authorization: 'Bearer editor-token' },
      method: 'POST',
    }),
  );
  expect(video).toHaveAttribute(
    'src',
    'https://worker.example.com/api/media/media-1',
  );
  expect(video).toHaveAttribute('controls');
  expect(video).toHaveAttribute('crossorigin', 'use-credentials');
  expect(createObjectUrl).not.toHaveBeenCalled();
});

it('renders a historical item without a media session or player', () => {
  const fetcher = vi.fn();
  vi.stubGlobal('fetch', fetcher);

  const { container } = render(
    <VideoPanel
      accessToken="editor-token"
      apiBaseUrl="https://worker.example.com"
      item={item}
      mediaMode="historical"
      role="editor"
      sessionMode="identity"
    />,
  );

  expect(container.querySelector('video')).toBeNull();
  expect(fetcher).not.toHaveBeenCalled();
  expect(screen.getByRole('heading', { name: 'source title' })).toBeVisible();
  expect(screen.getByText('source caption')).toBeVisible();
  expect(screen.getByText('v1')).toBeVisible();
});

it('links a historical item to its original video source', () => {
  render(
    <VideoPanel
      accessToken="editor-token"
      apiBaseUrl="https://worker.example.com"
      item={item}
      mediaMode="historical"
      role="viewer"
      sessionMode="identity"
    />,
  );

  expect(screen.getByRole('link', { name: '打开原视频' })).toHaveAttribute(
    'href',
    item.sourceUrl,
  );
});

it('does not render an external link for an empty historical source URL', () => {
  const { container } = render(
    <VideoPanel
      accessToken="editor-token"
      apiBaseUrl="https://worker.example.com"
      item={{ ...item, sourceUrl: '' }}
      mediaMode="historical"
      role="viewer"
      sessionMode="identity"
    />,
  );

  expect(container.querySelector('a')).toBeNull();
});

it('does not render the current-day source link for an empty source URL', () => {
  const { container } = render(
    <VideoPanel
      accessToken={null}
      apiBaseUrl="https://worker.example.com"
      item={{ ...item, sourceUrl: '' }}
      mediaMode="current_day"
      role="viewer"
      sessionMode="identity"
    />,
  );

  expect(container.querySelector('a')).toBeNull();
});

it('marks the video metadata transcript row with a semantic class', () => {
  const { container } = render(
    <VideoPanel
      accessToken={null}
      apiBaseUrl="https://worker.example.com"
      item={item}
      mediaMode="current_day"
      role="public_reader"
      sessionMode="public"
    />,
  );

  expect(container.querySelector('.video-metadata-transcript')).toHaveTextContent('文稿source caption');
});

it('creates a collaborator media session with cookie credentials and no bearer token', async () => {
  const fetcher = vi.fn(async () => new Response(null, { status: 204 }));
  vi.stubGlobal('fetch', fetcher);

  const { container } = render(
    <VideoPanel
      accessToken={null}
      apiBaseUrl="https://worker.example.com"
      item={item}
      mediaMode="current_day"
      role="collaborator"
      sessionMode="collaborator"
    />,
  );

  await waitFor(() => {
    expect(container.querySelector('video')).not.toBeNull();
  });

  expect(fetcher).toHaveBeenCalledWith(
    'https://worker.example.com/api/media/session',
    expect.objectContaining({
      credentials: 'include',
      headers: {},
      method: 'POST',
    }),
  );
});
