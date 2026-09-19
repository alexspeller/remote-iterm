// Links in terminal output open on the phone, where the Mac's loopback
// addresses would mean the phone itself. A dev server that printed
// http://localhost:3000 is reachable from the phone at the Mac's address —
// the one this page was loaded from — so those hosts are rewritten to it.
const LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1', '0.0.0.0', '[::1]']);

/** The href to open for a link in terminal output, given the page's host. */
export function linkHref(url: string, pageHost: string): string {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return url;
  }
  if (!LOOPBACK_HOSTS.has(parsed.hostname.toLowerCase())) return url;
  parsed.hostname = pageHost;
  return parsed.toString();
}
