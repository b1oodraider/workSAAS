"""Request dependencies: DB session, current user, form -> params helpers."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, get_args, get_origin

from fastapi import Depends, Request
from pydantic import BaseModel
from pydantic_core import PydanticUndefined
from sqlalchemy.orm import Session

from app.core.db import session_scope
from app.models import User


class LoginRequired(Exception):
    pass


@dataclass
class CurrentUser:
    id: int
    username: str
    is_admin: bool


def db() -> Iterator[Session]:
    with session_scope() as s:
        yield s


def current_user(request: Request, s: Session = Depends(db)) -> CurrentUser:
    user_id = request.session.get("user_id")
    user = s.get(User, user_id) if user_id else None
    if user is None or not user.is_active:
        request.session.clear()
        raise LoginRequired()
    cu = CurrentUser(id=user.id, username=user.username, is_admin=user.is_admin)
    request.state.user = cu
    return cu


def safe_path(value: str, default: str = "/") -> str:
    """Only allow local redirect targets (no open redirects)."""
    return value if value.startswith("/") and not value.startswith("//") else default


def params_from_form(model: type[BaseModel], form: Any, prefix: str = "p_") -> dict[str, Any]:
    """Collect ``p_<field>`` form values for a feature params model."""
    data: dict[str, Any] = {}
    for name, field in model.model_fields.items():
        key = prefix + name
        ann = field.annotation
        if ann is bool:
            data[name] = key in form
        elif key in form and form[key] != "":
            data[name] = form[key]
        elif get_origin(ann) is None and ann is str and key in form:
            data[name] = ""
    return data


def form_fields(model: type[BaseModel]) -> list[dict[str, Any]]:
    """Describe params model fields for the generic form macro."""
    fields = []
    for name, field in model.model_fields.items():
        ann = field.annotation
        extra = field.json_schema_extra if isinstance(field.json_schema_extra, dict) else {}
        item: dict[str, Any] = {
            "name": name,
            "title": field.title or name,
            "default": field.default if field.default not in (None, PydanticUndefined) else "",
            "required": field.is_required(),
            "placeholder": extra.get("placeholder", ""),
        }
        if extra.get("widget") == "textarea":
            item["type"] = "textarea"
        elif get_origin(ann) is not None and get_args(ann) and all(isinstance(a, str) for a in get_args(ann)):
            item["type"] = "select"
            labels = extra.get("labels") or {}
            item["options"] = [(o, labels.get(o, o)) for o in get_args(ann)]
        elif ann is bool:
            item["type"] = "checkbox"
        elif ann is int:
            item["type"] = "number"
        else:
            item["type"] = "text"
        fields.append(item)
    return fields
