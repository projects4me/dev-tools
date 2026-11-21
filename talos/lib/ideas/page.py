"""The HTML page a UI screen is rendered from: its stylesheets, and Prometheus' app shell around the screen.

The shell is a static copy of what Prometheus renders for app-layouts/application-header.hbs (with
project-switcher.hbs and the header avatar), application-sidebar.hbs with app-ui/nav-bar.hbs and navbar-items.hbs,
and application-footer.hbs, with the navigation of utils/metadata/metadata.js (Navigation.items, adminItems) and
the labels of translations/en-us.json. Keep it in step when those change; docs/ui/screens/list-projects.png shows
it. A screen may bring its own <aside class="main-sidebar"> (screens.py), which replaces the sidebar.
shell="none" renders the screen alone. The screen's HTML and CSS are already sanitised (screens.py); everything
else put into the page is escaped. The screen's CSS comes after the stylesheets, so its own classes are styled in
both shells.
"""

import html
from datetime import date
from pathlib import Path

from .styles import KIT

# (icon, label, sub-menu labels) per group, in the sidebar's order
WORKSPACE_NAV = (("dashboard", "Dashboard", ()), ("briefcase", "Projects", ("List Projects", "Create Project")))
PROJECT_NAV = (("tasks", "Issues", ("List Issues", "Create Issue")), ("columns", "Task Board", ()),
               ("calendar", "Gantt Cal", ()), ("comments", "Threads", ()), ("lightbulb-o", "Ideas", ()))
ADMIN_NAV = (("sitemap", "Workflows", ()), ("android", "Agents", ("List Agents", "Create Agent", "Tool Catalog",
                                                                  "Skills")),
             ("cogs", "Settings", ()), ("code", "Developer", ()))
CHROME_HEIGHT = 101     # header 50 + footer 51: AdminLTE sizes the content so the footer ends the window


def _item(icon, label, actions, disabled=False):
    """One navbar-items.hbs entry: a link, a treeview with its sub-menu, or a disabled project item."""
    if disabled:
        return (f'<li class="nav-disabled" title="Select a project first"><a role="link" aria-disabled="true">'
                f'<i class="fa fa-{icon}"></i>\n<span>{label}</span></a></li>')
    if not actions:
        return f'<li class="treeview"><a href="#"><i class="fa fa-{icon}"></i>\n<span>{label}</span></a></li>'
    sub = "".join(f'<li><a href="#"><i class="fa fa-circle-o"></i>{a}</a></li>' for a in actions)
    return (f'<li class="treeview"><a href="#"><i class="fa fa-{icon}"></i>\n<span>{label}</span>'
            f'<span class="pull-right-container"><i class="fa fa-angle-left pull-right"></i></span></a>'
            f'<ul class="treeview-menu">{sub}</ul></li>')


def _sidebar(code):
    project_code = (f'<span class="nav-project-code">{code}</span>' if code else
                    '<span class="nav-project-code nav-project-none">Select a project</span>')
    return ('<aside class="main-sidebar"><section class="sidebar"><ul class="sidebar-menu">'
            '<li class="header">Workspace</li>' + "".join(_item(*i) for i in WORKSPACE_NAV) +
            '<li class="nav-divider" role="separator"></li>'
            f'<li class="header" data-navigation-group="project"><span>Project</span>{project_code}</li>' +
            "".join(_item(*i, disabled=not code) for i in PROJECT_NAV) +
            '<li class="nav-divider" role="separator"></li><li class="header">Admin</li>' +
            "".join(_item(*i) for i in ADMIN_NAV) + "</ul></section></aside>")


def _initials(name):
    words = name.split()
    return "".join(w[0] for w in words[:2]).upper() if words else ""


def _header(logo, who, project, code):
    switcher = (f'<span class="ember-power-select-selected-item"><span class="project-switcher-name">{project}'
                f'</span>\n<span class="project-switcher-code">{code}</span></span>' if project else
                '<span class="ember-power-select-placeholder">Select a project</span>')
    initials = _initials(html.unescape(who))
    avatar = (f'<span class="app-ui-avatar app-ui-avatar-s header-user-avatar"><span class="app-ui-avatar-initials '
              f'app-ui-avatar--c0 app-ui-avatar--p0"><span class="app-ui-avatar-pattern '
              f'app-ui-avatar-pattern--ring app-ui-avatar--a1"></span><span class="app-ui-avatar-text">'
              f'{html.escape(initials)}</span></span></span>')
    return f"""<header class="main-header">
<a href="#" class="logo"><span class="logo-mini"><img alt="" src="{logo}"></span><span class="logo-lg"><img alt="" src="{logo}"><b>Projects</b>4<b>Me</b></span></a>
<nav class="navbar navbar-static-top" role="navigation"><a href="#" class="sidebar-toggle" role="button"><span class="sr-only">Toggle navigation</span></a>
<div class="project-switcher"><div class="ember-basic-dropdown-trigger ember-power-select-trigger">{switcher}<span class="ember-power-select-status-icon"></span></div></div>
<div class="global-search"><div class="ember-basic-dropdown-trigger ember-power-select-trigger"><span class="ember-power-select-placeholder">Search ...</span><span class="ember-power-select-status-icon"></span></div></div>
<div class="navbar-custom-menu"><ul class="nav navbar-nav">
<li><div class="create-modules"><button type="button" class="create-module-btn btn btn-primary"><i class="fa fa-plus visible-xs-inline"></i><span class="create-module-label hidden-xs">New</span><i class="fa fa-caret-down hidden-xs"></i></button></div></li>
<li class="dropdown notifications-menu"><button type="button" class="toggle-notifications-sidebar btn"><i class="fa fa-bell-o"></i></button></li>
<li class="dropdown user user-menu"><a href="#" class="dropdown-toggle"><span class="header-avatar-status header-user-avatar-wrap">{avatar}<i class="header-status-dot" aria-hidden="true"></i></span>
<span id="user-name" class="hidden-xs">{who}</span></a></li>
</ul></div></nav></header>"""


def _footer():
    return (f'<footer class="main-footer"><div class="pull-right hidden-xs"><i class="fa fa-bug"></i> <a href="#">'
            f'Report Issue</a></div><strong data-footer="copyright">Copyright © {date.today().year} '
            f'<a href="#">Projects4Me Inc.</a>.</strong> All rights reserved.</footer>')


def build(screen, css, engineer=""):
    links = "\n".join(f'<link rel="stylesheet" href="{html.escape(href, quote=True)}">' for href in css)
    if screen.get("css"):
        links += f"\n<style>\n{screen['css'].replace('<', '')}\n</style>"     # css.clean_css never keeps '<'
    body_class = html.escape(screen.get("body") or "", quote=True)
    if screen["shell"] == "none":
        body = f'<body class="{body_class}" style="background:#ecf0f5;padding:20px">{screen["html"]}</body>'
        return f'<!doctype html>\n<html><head><meta charset="utf-8">\n{links}\n</head>\n{body}\n</html>\n'
    logo = Path(KIT / "logo.png").resolve().as_uri()
    who = html.escape(engineer or "Engineer")
    project = html.escape(screen.get("project") or "")
    code = html.escape(screen.get("code") or "")
    sidebar = screen.get("sidebar") or _sidebar(code)
    body = f"""<body class="skin-black-light sidebar-mini {body_class}"><div class="wrapper">
{_header(logo, who, project, code)}
{sidebar}
<div class="content-wrapper" style="min-height:{max(0, int(screen["height"]) - CHROME_HEIGHT)}px">{screen["html"]}</div>
{_footer()}
</div></body>"""
    return f'<!doctype html>\n<html><head><meta charset="utf-8">\n{links}\n</head>\n{body}\n</html>\n'
