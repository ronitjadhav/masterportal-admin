"""Role-based access decisions and config filtering (Phase 3).

Deny-by-default: a secured (is_public=False) service is usable only by roles
granted in service_roles — or by the `admin` role. Anonymous callers never
reach secured services. Filtering the served configs is UX (users don't see
layers/modules they can't use); the enforced boundary is the proxy's 401/403,
which uses the same decision function.
"""
import copy

from .auth import roles as token_roles

ADMIN_ROLE = "admin"


def user_roles(user: dict | None) -> set[str] | None:
    """None = anonymous; otherwise the caller's role set."""
    return None if user is None else token_roles(user)


def service_allowed(is_public: bool, grants: set[str], uroles: set[str] | None) -> bool:
    if is_public:
        return True
    if uroles is None:
        return False
    return ADMIN_ROLE in uroles or bool(grants & uroles)


def roles_satisfy(required: set[str], uroles: set[str] | None) -> bool:
    """For portal/module restrictions: empty requirement = open to all."""
    if not required:
        return True
    if uroles is None:
        return False
    return ADMIN_ROLE in uroles or bool(required & uroles)


def filter_layer_config(layer_config: dict, known_ids: set[str], allowed_ids: set[str]) -> dict:
    """Prune tree elements whose service the caller may not use.

    Ids can carry suffixes ("8712.1" → service "8712"); GROUP layers carry id
    lists and are kept only if ALL members are allowed (a partial group would
    render broken). Ids not in the catalog at all (inline layer definitions)
    are kept — they carry their own config and cannot be gated here.
    """
    def id_ok(lid) -> bool:
        base = str(lid).split(".")[0]
        return base not in known_ids or base in allowed_ids

    def prune(elements: list) -> list:
        kept = []
        for el in elements or []:
            if el.get("type") == "folder":
                children = prune(el.get("elements"))
                if children:
                    kept.append({**el, "elements": children})
            else:
                el_id = el.get("id")
                ids = el_id if isinstance(el_id, list) else [el_id]
                if all(id_ok(i) for i in ids):
                    kept.append(el)
        return kept

    filtered = copy.deepcopy(layer_config)
    for group in filtered.values():
        if isinstance(group, dict) and "elements" in group:
            group["elements"] = prune(group["elements"])
    return filtered


def filter_portal_config(portal_config: dict, restricted: dict[str, set[str]],
                         uroles: set[str] | None) -> dict:
    """Drop restricted menu modules the caller's roles don't satisfy."""
    if not restricted:
        return portal_config

    def module_ok(el: dict) -> bool:
        return roles_satisfy(restricted.get(el.get("type"), set()), uroles)

    filtered = copy.deepcopy(portal_config)
    for menu in ("mainMenu", "secondaryMenu"):
        sections = filtered.get(menu, {}).get("sections")
        if isinstance(sections, list):
            filtered[menu]["sections"] = [
                [el for el in section if module_ok(el)] for section in sections
            ]
    return filtered
