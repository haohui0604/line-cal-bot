# -*- coding: utf-8 -*-
"""Phase 12: トレーナー未設定の会員には質問ボタン/コメント欄を出さない."""
import os, sys, pathlib, tempfile
os.environ.setdefault("DB_PATH", tempfile.mkdtemp() + "/phase12.db")
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest
from fastapi import HTTPException
from jinja2 import Environment, FileSystemLoader

TPL = ROOT / "app" / "templates"


def _render(**ctx):
    env = Environment(loader=FileSystemLoader(str(TPL)), autoescape=True)
    base = {"request": None, "member_id": None, "can_comment": False,
            "can_edit": True, "liff_id": "1234567890-abc",
            "initial_date": "2026-10-09"}
    base.update(ctx)
    return env.get_template("day_detail.html").render(**base)


def test_member_view_has_gated_containers():
    html = _render(member_id=None, can_comment=False)
    assert 'id="askCard"' in html and "display:none" in html
    assert 'id="cmtCard"' in html
    assert 'id="commentBody"' not in html


def test_staff_view_has_comment_form_and_no_ask_button():
    html = _render(member_id="U_member", can_comment=True)
    assert 'id="commentBody"' in html
    assert 'id="askCard"' not in html
    assert 'id="cmtCard"' not in html


def test_js_reveals_only_when_has_trainer():
    html = _render(member_id=None, can_comment=False)
    assert "__gate_trainer__" in html and "d.has_trainer" in html


def test_question_api_blocked_without_trainer(monkeypatch):
    from app.web import day as day_mod
    monkeypatch.setattr(day_mod, "_resolve_viewer", lambda *a, **k: ("U1", False, None))
    monkeypatch.setattr(day_mod.gym_db, "has_trainer", lambda u: False)
    body = day_mod.EntryOpIn(action="question", date="2026-10-09", body="夕食の量は？")
    with pytest.raises(HTTPException) as e:
        day_mod.day_entry_ops(body, None)
    assert e.value.status_code == 400
    assert "担当トレーナー" in e.value.detail


def test_question_api_allowed_with_trainer(monkeypatch):
    from app.web import day as day_mod
    monkeypatch.setattr(day_mod, "_resolve_viewer", lambda *a, **k: ("U1", False, None))
    monkeypatch.setattr(day_mod.gym_db, "has_trainer", lambda u: True)
    monkeypatch.setattr(day_mod.gym_db, "add_comment", lambda **k: 1)
    monkeypatch.setattr(day_mod.gym_db, "get_member_trainer", lambda u: "T1")
    monkeypatch.setattr(day_mod.gym_db, "get_user", lambda u: {"display_name": "先生"})
    monkeypatch.setattr(day_mod, "_push_to_member", lambda *a, **k: None)
    body = day_mod.EntryOpIn(action="question", date="2026-10-09", body="夕食の量は？")
    r = day_mod.day_entry_ops(body, None)
    assert r["ok"] is True and r["pushed"] is True
