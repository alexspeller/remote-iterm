// Notification deep links.
//
// `notify` on the Mac puts `http://<host>:7292/#session=<iTerm session id>` on
// each push notification; tapping it opens this page at that fragment. The id
// is the UUID from ITERM_SESSION_ID, which is also the pane id the server
// reports, so the page only has to find it in the window/tab tree. It lives in
// the fragment — next to the access key from the QR URL — so it is never sent
// to the server and needs no routing.
import type { WindowState } from './types';

export const DEEP_LINK_PARAM = 'session';

export interface SessionLocation {
  windowId: string;
  tabId: string;
  tabIndex: number;
  sessionId: string;
}

function fragmentParams(hash: string): URLSearchParams {
  return new URLSearchParams(hash.replace(/^#/, ''));
}

/** The pane a deep link asks for, if the fragment names one. */
export function deepLinkSessionId(hash: string): string | null {
  return fragmentParams(hash).get(DEEP_LINK_PARAM)?.trim() || null;
}

/** Where a pane lives, or null once it has been closed. */
export function findSessionLocation(state: WindowState[], sessionId: string): SessionLocation | null {
  for (const win of state) {
    for (const tab of win.tabs) {
      if (tab.sessions.some(session => session.id === sessionId)) {
        return { windowId: win.id, tabId: tab.id, tabIndex: tab.index, sessionId };
      }
    }
  }
  return null;
}

/**
 * The fragment with the deep link removed and everything else (the access key)
 * kept, so a later reload of the bookmarked page does not jump back to the pane.
 */
export function stripDeepLink(hash: string): string {
  const params = fragmentParams(hash);
  params.delete(DEEP_LINK_PARAM);
  const rest = params.toString();
  return rest ? `#${rest}` : '';
}
