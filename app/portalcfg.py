"""Portal creation + structured editing of portalConfig.

A "portal" is its config.json (portalConfig + layerConfig). This module knows
how to (a) build a minimal valid starter, (b) read a structured settings view
out of portalConfig for the admin forms, and (c) merge edits back in without
disturbing the parts the forms don't cover. Everything operates on plain dicts;
persistence/auditing lives in admin.py.

Field paths follow docs/User/Portal-Config/config.json.md (config.json v3).
"""
import copy

# Map controls the settings form can toggle (config.json.md:114). Each is a
# plain boolean in portalConfig.map.controls (some accept an object; we only
# toggle presence/boolean here — advanced object config goes via raw JSON).
CONTROLS = ["zoom", "orientation", "fullScreen", "totalView", "rotation",
            "backForward", "button3d"]

# Menu modules safe to enable with little/no required config. needs_config=True
# means the module renders but won't work until configured (raw-JSON tier).
MODULES = {
    "about": {"label": "About / imprint"},
    "legend": {"label": "Legend"},
    "contact": {"label": "Contact form", "needs_config": True},
    "shareView": {"label": "Share view"},
    "measure": {"label": "Measure"},
    "draw": {"label": "Draw / annotate"},
    "coordToolkit": {"label": "Coordinates"},
    "scaleSwitcher": {"label": "Scale switcher"},
    "fileImport": {"label": "File import (KML/GeoJSON/GPX)"},
    "openConfig": {"label": "Load config at runtime"},
    "selectFeatures": {"label": "Select features"},
    "language": {"label": "Language switch"},
    "news": {"label": "News feed"},
    "shadow": {"label": "3D shadow", "needs_config": True},
    "styleVT": {"label": "Vector-tile style switch"},
    "compareMaps": {"label": "Compare maps (swipe)"},
    "layerClusterToggler": {"label": "Cluster toggler", "needs_config": True},
    "print": {"label": "Print (PDF)", "needs_config": True},
    "measure3d": {"label": "3D measure", "needs_config": True},
    "login": {"label": "Login"},
}


def starter(title: str) -> dict:
    """Smallest useful, valid config.json for a brand-new portal."""
    return {
        "portalConfig": {
            "map": {
                "startingMapMode": "2D",
                "mapView": {"startCenter": [561210, 5932600], "startZoomLevel": 1,
                            "epsg": "EPSG:25832"},
                "controls": {"zoom": True, "orientation": {"zoomMode": "once"}},
            },
            "mainMenu": {"expanded": True, "title": {"text": title},
                         "sections": [[{"type": "about"}, {"type": "login"}]]},
            "secondaryMenu": {"sections": [[]]},
            "portalFooter": {"urls": []},
            "tree": {},
        },
        "layerConfig": {"baselayer": {"elements": []}, "subjectlayer": {"elements": []}},
    }


def _menu_module_types(menu: dict) -> list[str]:
    types = []
    for section in menu.get("sections", []) or []:
        for el in section:
            t = el.get("type")
            if t and t != "folder":
                types.append(t)
    return types


def extract_settings(pc: dict) -> dict:
    """Structured, form-friendly view of the parts of portalConfig we edit."""
    mainmenu = pc.get("mainMenu", {})
    title = mainmenu.get("title", {}) or {}
    mapc = pc.get("map", {})
    view = mapc.get("mapView", {}) or {}
    controls = mapc.get("controls", {}) or {}
    return {
        "title": {k: title.get(k, "") for k in ("text", "logo", "link", "toolTip")},
        "footerUrls": (pc.get("portalFooter", {}) or {}).get("urls", []) or [],
        "map": {
            "startingMapMode": mapc.get("startingMapMode", "2D"),
            "startCenter": view.get("startCenter", []),
            "startZoomLevel": view.get("startZoomLevel"),
            "extent": view.get("extent", []),
            "epsg": view.get("epsg", ""),
            "backgroundImage": view.get("backgroundImage", ""),
        },
        "controls": {c: bool(controls.get(c)) for c in CONTROLS},
        "modules": {
            "mainMenu": _menu_module_types(pc.get("mainMenu", {})),
            "secondaryMenu": _menu_module_types(pc.get("secondaryMenu", {})),
        },
    }


def apply_settings(pc: dict, patch: dict) -> dict:
    """Merge a structured settings patch into portalConfig (returns a new dict).

    Only the keys present in `patch` are touched; everything else is preserved.
    """
    pc = copy.deepcopy(pc)
    if "title" in patch:
        title = pc.setdefault("mainMenu", {}).setdefault("title", {})
        for k in ("text", "logo", "link", "toolTip"):
            if k in patch["title"]:
                v = patch["title"][k]
                if v:
                    title[k] = v
                else:
                    title.pop(k, None)
    if "footerUrls" in patch:
        pc.setdefault("portalFooter", {})["urls"] = patch["footerUrls"]
    if "map" in patch:
        m = patch["map"]
        mapc = pc.setdefault("map", {})
        view = mapc.setdefault("mapView", {})
        if "startingMapMode" in m:
            mapc["startingMapMode"] = "3D" if m["startingMapMode"] == "3D" else "2D"
        for k in ("startCenter", "extent"):
            if k in m and m[k]:
                view[k] = m[k]
        if "startZoomLevel" in m and m["startZoomLevel"] is not None:
            view["startZoomLevel"] = m["startZoomLevel"]
        for k in ("epsg", "backgroundImage"):
            if k in m:
                if m[k]:
                    view[k] = m[k]
                else:
                    view.pop(k, None)
    if "controls" in patch:
        controls = pc.setdefault("map", {}).setdefault("controls", {})
        for c, on in patch["controls"].items():
            if c not in CONTROLS:
                continue
            if on:
                controls.setdefault(c, True)   # don't clobber an existing object config
            else:
                controls.pop(c, None)
    return pc


def set_module(pc: dict, menu: str, module_type: str, enabled: bool) -> dict:
    """Enable/disable a menu module. Enabling appends to the menu's last
    section; disabling removes every instance of that type from all sections.
    (Reorder + per-module config are a later tier.)"""
    if menu not in ("mainMenu", "secondaryMenu"):
        raise ValueError("menu must be mainMenu or secondaryMenu")
    if module_type not in MODULES:
        raise ValueError(f"unknown module {module_type}")
    pc = copy.deepcopy(pc)
    m = pc.setdefault(menu, {})
    sections = m.setdefault("sections", [[]]) or [[]]
    if not sections:
        sections.append([])
    if enabled:
        if module_type not in _menu_module_types(m):
            sections[-1].append({"type": module_type})
    else:
        for section in sections:
            section[:] = [el for el in section if el.get("type") != module_type]
    m["sections"] = sections
    return pc


def module_catalog() -> list[dict]:
    return [{"type": t, **info} for t, info in MODULES.items()]
