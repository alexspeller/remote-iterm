import { describe, expect, it } from 'vitest';
import { deepLinkSessionId, findSessionLocation, stripDeepLink } from './deepLink';
import type { WindowState } from './types';

const SESSION = '923ED3AF-1F1B-4EC0-AEF0-1DAFC4E942F0';

const state: WindowState[] = [
  {
    id: 'window-1',
    isFront: true,
    tabs: [
      { index: 1, id: 'window-1-1', isSelected: true, sessions: [{ id: 'front-pane', name: 'shell' }] },
    ],
  },
  {
    id: 'window-2',
    isFront: false,
    tabs: [
      { index: 1, id: 'window-2-1', isSelected: false, sessions: [{ id: 'other', name: 'vim' }] },
      {
        index: 2,
        id: 'window-2-2',
        isSelected: true,
        currentSessionId: 'left',
        sessions: [{ id: 'left', name: 'yarn deck' }, { id: SESSION, name: 'claude' }],
      },
    ],
  },
];

describe('deepLinkSessionId', () => {
  it('reads the session next to the access key in the fragment', () => {
    expect(deepLinkSessionId(`#key=abc&session=${SESSION}`)).toBe(SESSION);
    expect(deepLinkSessionId(`#session=${SESSION}`)).toBe(SESSION);
  });

  it('is null when the fragment carries no session', () => {
    expect(deepLinkSessionId('')).toBeNull();
    expect(deepLinkSessionId('#key=abc')).toBeNull();
    expect(deepLinkSessionId('#session=')).toBeNull();
    expect(deepLinkSessionId('#session=%20')).toBeNull();
  });
});

describe('findSessionLocation', () => {
  it('locates a background split pane in a non-front window', () => {
    expect(findSessionLocation(state, SESSION)).toEqual({
      windowId: 'window-2',
      tabId: 'window-2-2',
      tabIndex: 2,
      sessionId: SESSION,
    });
  });

  it('is null for a pane that has been closed', () => {
    expect(findSessionLocation(state, 'gone')).toBeNull();
    expect(findSessionLocation([], SESSION)).toBeNull();
  });
});

describe('stripDeepLink', () => {
  it('keeps the access key and drops only the session', () => {
    expect(stripDeepLink(`#key=abc_-123&session=${SESSION}`)).toBe('#key=abc_-123');
    expect(stripDeepLink(`#session=${SESSION}&key=abc`)).toBe('#key=abc');
  });

  it('leaves no dangling hash when nothing else was there', () => {
    expect(stripDeepLink(`#session=${SESSION}`)).toBe('');
    expect(stripDeepLink('')).toBe('');
  });
});
