"""Friday as a Mac app, living in the menu bar.

"Open a browser tab and keep it open" is not a product a person adopts. A tab
gets closed by accident, loses its place among thirty others, and stops existing
the moment the browser is quit. Friday's whole claim is being the one place you
go, and the one place you go cannot be the eleventh tab.

So: a menu bar item, permanently present, showing a count of what needs you, with
the real page one click away. Not a separate interface. The same server, the same
HTML, the same conversation, inside a WKWebView; anything else would be a second
thing to keep in sync and it would drift within a week.

**Why not a packaged app.** Electron and Tauri each buy a nicer icon and cost a
build pipeline, a signing identity, a notarisation step and a second toolchain.
pyobjc is already a dependency here for the calendar, WKWebView comes with the
system, and the result launches from source with no build and no Apple developer
account. If this ever ships to strangers that trade changes; today it does not,
and paying for it now would be paying early for the wrong thing.

**What it deliberately does not do.** It does not become a second notification
channel. Web Push already handles alerts, including with the page closed and the
phone locked, and adding NSUserNotification on top would mean two systems with
two mute switches disagreeing about whether you have been told. The menu bar
count is a glance, not an interruption.

    python3 -m friday.app        the app
    python3 run.py               the server on its own, as before
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import objc
from AppKit import (NSApp, NSApplication, NSApplicationActivationPolicyAccessory,
                    NSColor, NSEvent, NSImage, NSMakeRect, NSMenu, NSMenuItem,
                    NSPopover, NSPopoverBehaviorTransient, NSStatusBar,
                    NSVariableStatusItemLength, NSViewController,
                    NSWorkspace)
from Foundation import (NSURL, NSURLRequest, NSObject, NSTimer)

objc.loadBundle("WebKit", globals(),
                bundle_path="/System/Library/Frameworks/WebKit.framework")

PORT = int(os.environ.get("FRIDAY_PORT", "8765"))
HOME = f"http://127.0.0.1:{PORT}"
POLL = 4.0
# Roughly a phone held in portrait, which is the shape the page was designed
# around and the shape a glance wants. Wider makes the conversation harder to
# read, not easier.
SIZE = (420, 640)


def _secret() -> str:
    try:
        return (Path.home() / ".friday" / "secret").read_text().strip()
    except Exception:
        return ""


def _url() -> str:
    tok = _secret()
    return f"{HOME}/?k={tok}" if tok else HOME


# ------------------------------------------------------------ the server ----
def _up(timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(f"{HOME}/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _state() -> dict:
    """What needs you, from the server Friday is already running.

    Read over HTTP rather than by importing the engine, because the app must
    not become a second Friday: two of them polling the same fleet would double
    every announcement and disagree about what has been said."""
    tok = _secret()
    url = f"{HOME}/state" + (f"?k={tok}" if tok else "")
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return json.loads(r.read())
    except Exception:
        return {}


class Server:
    """The Friday server, started by the app if it is not already running.

    Adopting an existing one matters more than it sounds: people run `python3
    run.py` in a terminal and then open the app, and a second server would bind
    a second port, watch the same sessions and announce everything twice."""

    def __init__(self):
        self.proc = None

    def ensure(self) -> None:
        if _up():
            return                      # somebody else's, and that is fine
        root = Path(__file__).resolve().parents[1]
        self.proc = subprocess.Popen(
            [sys.executable, str(root / "run.py"), str(PORT)],
            cwd=str(root), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        for _ in range(60):
            if _up(0.5):
                return
            time.sleep(0.25)

    def stop(self) -> None:
        # Only what this app started. Killing a server somebody is using from a
        # terminal because they quit the menu bar item would be rude and
        # surprising in equal measure.
        if not self.proc:
            return
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except Exception:
            try:
                self.proc.terminate()
            except Exception:
                pass
        self.proc = None


# ----------------------------------------------------------- the popover ----
class WebController(NSViewController):
    """The real page, in a window that is not a browser tab."""

    def loadView(self):
        cfg = WKWebViewConfiguration.alloc().init()
        # The page talks to 127.0.0.1 over plain HTTP and asks for
        # notifications; both are fine for a local app and neither is the
        # default in a fresh web view.
        try:
            cfg.preferences().setValue_forKey_(True,
                                               "allowFileAccessFromFileURLs")
        except Exception:
            pass
        view = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, *SIZE), cfg)
        view.setValue_forKey_(False, "drawsBackground")
        self.setView_(view)
        self.web = view
        self.reload()

    def reload(self):
        req = NSURLRequest.requestWithURL_(NSURL.URLWithString_(_url()))
        self.web.loadRequest_(req)


class App(NSObject):
    def init(self):
        self = objc.super(App, self).init()
        self.server = Server()
        self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength)
        self.item.button().setTitle_("F")
        self.item.button().setTarget_(self)
        self.item.button().setAction_("toggle:")
        self.item.button().sendActionOn_(1 << 1 | 1 << 3)   # left and right
        self.pop = NSPopover.alloc().init()
        self.ctrl = WebController.alloc().init()
        self.pop.setContentViewController_(self.ctrl)
        self.pop.setContentSize_(SIZE)
        self.pop.setBehavior_(NSPopoverBehaviorTransient)
        self.needs = -1
        return self

    # ---- what the menu bar says ------------------------------------------
    def refresh_(self, _timer=None):
        """The count, and nothing else.

        Deliberately one number. A menu bar is glanced at, not read, and a
        status line that needs reading is a status line that gets ignored. The
        number is only ever things WAITING ON YOU: a busy fleet is not news, a
        blocked one is."""
        st = _state()
        rows = st.get("sessions") or st.get("fleet") or []
        try:
            n = sum(1 for r in rows if (r or {}).get("status") == "needs")
        except Exception:
            n = 0
        if not st:
            title = "F?"                # the server is not answering
        elif n:
            title = f"F {n}"
        else:
            title = "F"
        if title != getattr(self, "_title", None):
            self._title = title
            self.item.button().setTitle_(title)

    def toggle_(self, sender):
        ev = NSEvent.pressedMouseButtons()
        if ev & (1 << 1):               # right click: the plain menu
            return self.showMenu_(sender)
        if self.pop.isShown():
            self.pop.performClose_(sender)
            return
        self.ctrl.reload()
        self.pop.showRelativeToRect_ofView_preferredEdge_(
            sender.bounds(), sender, 1)
        NSApp.activateIgnoringOtherApps_(True)

    def showMenu_(self, sender):
        menu = NSMenu.alloc().init()
        for title, action in (("Open in browser", "openBrowser:"),
                              ("Reload", "reloadPage:"),
                              (None, None),
                              ("Quit Friday", "quit:")):
            if title is None:
                menu.addItem_(NSMenuItem.separatorItem())
                continue
            it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                title, action, "")
            it.setTarget_(self)
            menu.addItem_(it)
        self.item.setMenu_(menu)
        self.item.button().performClick_(None)
        self.item.setMenu_(None)        # back to click-opens-the-panel

    def openBrowser_(self, _sender):
        NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(_url()))

    def reloadPage_(self, _sender):
        self.ctrl.reload()

    def quit_(self, _sender):
        self.server.stop()
        NSApp.terminate_(None)


def main():
    app = NSApplication.sharedApplication()
    # Accessory, so Friday has no Dock icon and no menu bar of its own. It is a
    # thing that sits there, not a window you manage.
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    delegate = App.alloc().init()
    threading.Thread(target=delegate.server.ensure, daemon=True).start()
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        POLL, delegate, "refresh:", None, True)
    delegate.refresh_()
    app.run()


if __name__ == "__main__":
    main()
