"""Keep the iTerm2 API cookie request out of this process.

Every connection to iTerm2's API starts with an AppleScript asking iTerm2 for
a cookie. When pyobjc is importable (it is a dependency here), the iterm2
library runs that AppleScript in-process through NSAppleScript. Sending Apple
Events that way registers the process with LaunchServices and WindowServer as
an app, and Homebrew's framework Python runs from inside Python.app, so it
gets one. The process's main thread then runs an asyncio loop, never a Cocoa
run loop, so it never answers WindowServer's events, and Activity Monitor
lists the long-lived server and snapshotter as "Python (Not Responding)" even
though both are working.

The library's other runner sends the same AppleScript through a short-lived
/usr/bin/osascript child, which leaves this process unregistered and so
outside WindowServer's responsiveness checks.
"""
import iterm2.auth


def use_osascript_for_cookie() -> None:
    """Make every later iterm2.auth.authenticate() call use osascript.

    Covers the library's own retry after a 401, which drops the cookie and
    requests a fresh one. The flag lives on iterm2.auth alone; iterm2.color
    has its own copy, so colour parsing still uses AppKit.
    test_iterm_cookie fails if an iterm2 upgrade stops honouring it.
    """
    iterm2.auth.gAppKitAvailable = False
