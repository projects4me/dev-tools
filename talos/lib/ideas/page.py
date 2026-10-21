"""The HTML page a UI screen is rendered from: its stylesheets, and Prometheus' app shell around the screen.

The shell is a static copy of what Prometheus renders for app-layouts/application-header.hbs,
application-sidebar.hbs and app-ui/nav-bar.hbs (keep it in step when those change). shell="none" renders the
screen alone. The screen's HTML is already sanitised (screens.py); everything put into the page is escaped.
"""

import html
from pathlib import Path

from .styles import KIT

NAV = (("dashboard", "Dashboard"), ("list", "Issues"), ("columns", "Task Board"), ("calendar", "Gantt Cal"),
       ("comments", "Threads"), ("briefcase", "Projects"))
ADMIN = (("users", "Users"), ("key", "Roles"))


def _items(entries):
    return "".join(f'<li class="treeview"><a href="#"><i class="fa fa-{icon}"></i> <span>{label}</span></a></li>'
                   for icon, label in entries)


def build(screen, css, engineer=""):
    links = "\n".join(f'<link rel="stylesheet" href="{html.escape(href, quote=True)}">' for href in css)
    body_class = html.escape(screen.get("body") or "", quote=True)
    if screen["shell"] == "none":
        body = f'<body class="{body_class}" style="background:#ecf0f5;padding:20px">{screen["html"]}</body>'
        return f'<!doctype html>\n<html><head><meta charset="utf-8">\n{links}\n</head>\n{body}\n</html>\n'
    logo = Path(KIT / "logo.png").resolve().as_uri()
    who = html.escape(engineer or "Engineer")
    project = html.escape(screen.get("project") or "A Prometheus")
    body = f"""<body class="skin-black-light sidebar-mini {body_class}"><div class="wrapper">
<header class="main-header">
<a href="#" class="logo"><span class="logo-mini"><img alt="" src="{logo}"></span><span class="logo-lg"><img alt="" src="{logo}"><b>Projects</b>4Me</span></a>
<nav class="navbar navbar-static-top" role="navigation"><a href="#" class="sidebar-toggle" role="button"><span class="sr-only">Toggle</span></a>
<div class="global-search"><div class="ember-basic-dropdown-trigger ember-power-select-trigger"><span class="ember-power-select-placeholder">Search ...</span><span class="ember-power-select-status-icon"></span></div></div>
<div class="navbar-custom-menu"><ul class="nav navbar-nav">
<li><div class="create-modules"><button class="create-module-btn btn btn-primary">New<i class="fa fa-caret-down"></i></button></div></li>
<li class="dropdown notifications-menu"><button type="button" class="toggle-notifications-sidebar btn"><i class="fa fa-bell"></i></button></li>
<li class="dropdown user user-menu"><a href="#" class="dropdown-toggle"><i class="fa fa-user-circle"></i> <span class="hidden-xs">{who}</span></a></li>
</ul></div></nav></header>
<aside class="main-sidebar"><section class="sidebar">
<div class="user-panel"><div class="pull-left image"><i class="fa fa-user-circle fa-3x text-muted"></i></div>
<div class="pull-left info"><p id="user-name">{who}</p><a href="#"><i class="fa fa-circle text-success"></i>Online</a></div></div>
<div class="sidebar-form"><div class="input-group"><div class="ember-basic-dropdown-trigger ember-power-select-trigger form-control"><span class="ember-power-select-selected-item">{project}</span><span class="ember-power-select-status-icon"></span></div></div></div>
<ul class="sidebar-menu"><li class="header">Main Navigation</li>{_items(NAV)}<li class="header">Admin</li>{_items(ADMIN)}</ul>
</section></aside>
<div class="content-wrapper" style="min-height:{int(screen["height"])}px">{screen["html"]}</div>
</div></body>"""
    return f'<!doctype html>\n<html><head><meta charset="utf-8">\n{links}\n</head>\n{body}\n</html>\n'
