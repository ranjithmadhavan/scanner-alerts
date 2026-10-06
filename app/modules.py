"""Registry of the parts of the app a super admin can grant to users.

To add a new area later (e.g. trading), add an entry here, create its router and
templates, and it will show up in the navigation and in the user-permissions form.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Module:
    key: str
    label: str
    path: str
    description: str
    icon: str  # name of an inline SVG in templates/partials/icons.html


MODULES: dict[str, Module] = {
    m.key: m
    for m in [
        Module("scanner", "Price alerts", "/alerts", "Watch stocks and get told when a level is hit", "bell"),
        Module("broker", "Broker", "/broker", "Connect a Zerodha Kite account for live prices", "link"),
        Module("notifications", "Notifications", "/notifications", "Choose where alerts are delivered", "send"),
        Module("oi", "Nifty OI", "/oi", "Nifty option open interest through the day, and what it says", "chart"),
    ]
}
