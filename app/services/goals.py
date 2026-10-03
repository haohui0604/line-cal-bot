"""目的設定ウィザード (Phase 3.5).

- 減量: 現在体重→目標体重→期間 → 消費カロリー(活動量実績 or Mifflin-St Jeor式)から
  目標摂取kcalを自動計算 → set_goal にも反映
- 減塩: 目標 6.0g/日（変更可）
- 筋肉増量: たんぱく質目標 = 体重×1.6g

ウィザードの状態はモジュール内メモリ保持（既存の _pending と同じ方式）。
"""
import logging
import re
from typing import Optional

from linebot.models import TextSendMessage

from app.handlers.button_builder import qr, msq
from app.services.dates import today_jst
from app.services.db import (
    get_conn, set_goal, fetch_latest_weight,
)

logger = logging.getLogger(__name__)

FAT_KCAL_PER_KG = 7200
ACTIVITY_FACTOR = 1.4          # 活動量データが無い人の推定係数（軽い運動）
MIN_TARGET_KCAL = 1000.0       # 安全側の下限
DEFAULT_SALT_G = 6.0
PROTEIN_PER_KG = 1.6
FAT_RATIO = 0.25               # 目標kcalのうち脂質でとる目安の割合
FAT_KCAL_PER_G = 9
PROTEIN_KCAL_PER_G = 4
CARB_KCAL_PER_G = 4

_pending: dict = {}            # user_id -> {"step": ..., ...}


# ---------- DB ----------

def save_profile(user_id: str, **fields) -> None:
    cols = ["goal_mode", "target_weight_kg", "goal_days", "calc_target_kcal",
            "salt_target_g", "protein_target_g", "sex", "age", "height_cm"]
    sets = {k: v for k, v in fields.items() if k in cols}
    with get_conn() as c:
        c.execute(
            "INSERT INTO goal_profiles (user_id) VALUES (?)"
            " ON CONFLICT(user_id) DO NOTHING", (user_id,))
        if sets:
            assigns = ", ".join(f"{k}=?" for k in sets)
            c.execute(
                f"UPDATE goal_profiles SET {assigns},"
                " updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
                (*sets.values(), user_id))


def get_profile(user_id: str) -> Optional[dict]:
    with get_conn() as c:
        r = c.execute(
            "SELECT * FROM goal_profiles WHERE user_id=?", (user_id,)
        ).fetchone()
    return dict(r) if r else None


GOAL_JP = {"weight": "減量", "salt": "減塩", "muscle": "筋肉"}


def _badge_from_row(p: Optional[dict]):
    """goal_profiles の1行から一覧表示用バッジを作る（未設定なら None）."""
    if not p:
        return None
    mode = p.get("goal_mode")
    if mode not in GOAL_JP:
        return None
    detail = ""
    if mode == "weight":
        tw, gd = p.get("target_weight_kg"), p.get("goal_days")
        if tw and gd:
            detail = f"{float(tw):.1f}kg / {int(gd)}日"
        elif tw:
            detail = f"{float(tw):.1f}kg"
    elif mode == "salt":
        try:
            detail = f"{float(p.get('salt_target_g') or DEFAULT_SALT_G):.1f}g/日"
        except (TypeError, ValueError):
            detail = ""
    elif mode == "muscle":
        pt = p.get("protein_target_g")
        try:
            detail = f"P{float(pt):.0f}g/日" if pt else ""
        except (TypeError, ValueError):
            detail = ""
    return {"mode": mode, "label": GOAL_JP[mode], "detail": detail}


def goal_badge(user_id: str):
    """1会員の目的バッジ。設定していなければ None（一覧では「—」表示）."""
    try:
        return _badge_from_row(get_profile(user_id))
    except Exception:
        return None


def list_goal_badges(user_ids) -> dict:
    """複数会員ぶんの目的バッジをまとめて返す（未設定の会員は含めない）."""
    ids = [u for u in (user_ids or []) if u]
    if not ids:
        return {}
    out = {}
    try:
        with get_conn() as c:
            q = ",".join("?" for _ in ids)
            rows = c.execute(
                f"SELECT * FROM goal_profiles WHERE user_id IN ({q})", ids
            ).fetchall()
    except Exception:
        return {}
    for r in rows:
        d = dict(r)
        b = _badge_from_row(d)
        if b:
            out[d["user_id"]] = b
    return out


def context_line(user_id: str) -> str:
    """コメント生成のコンテキストに載せる1行。未設定なら空文字.

    目的に加えて、目安となる目標PFC（g）も添える。これがあることで
    AIコメントが「脂質が多い／たんぱく質が足りない」を根拠つきで書ける。
    """
    p = get_profile(user_id)
    if not p or not p.get("goal_mode"):
        return ""
    if p["goal_mode"] == "weight":
        tw = p.get("target_weight_kg")
        kcal = p.get("calc_target_kcal")
        s = "目的: 減量"
        if tw:
            s += f"（目標 {tw}kg / {p.get('goal_days') or '?'}日）"
        if kcal:
            s += f"。1日の目標摂取 {kcal:.0f}kcal"
    elif p["goal_mode"] == "salt":
        s = f"目的: 減塩（食塩目標 {p.get('salt_target_g') or DEFAULT_SALT_G}g/日）"
    elif p["goal_mode"] == "muscle":
        pt = p.get("protein_target_g")
        s = (f"目的: 筋肉増量（たんぱく質目標 {pt:.0f}g/日）" if pt
             else "目的: 筋肉増量（たんぱく質重視）")
    else:
        return ""
    t = pfc_targets(user_id, target_kcal=p.get("calc_target_kcal"))
    if t:
        s += "。目安PFC " + " / ".join(
            f"{name}{v:.0f}g" for name, v in
            (("P", t.get("protein_g")), ("F", t.get("fat_g")),
             ("C", t.get("carb_g"))) if v)
    return s


# ---------- 目標PFC（マスタ＋目安計算） ----------

def _table_cols(table: str) -> set:
    """既存DBに無いカラムで落ちないよう、実際に存在する列だけを返す."""
    try:
        with get_conn() as c:
            rows = c.execute(f"PRAGMA table_info({table})").fetchall()
    except Exception:
        logger.exception("PRAGMA table_info failed: %s", table)
        return set()
    cols = set()
    for r in rows:
        try:
            cols.add(r["name"])
        except (TypeError, KeyError, IndexError):
            cols.add(r[1])
    return cols


def save_pfc_targets(user_id: str, *, fat_target_g=None,
                     carb_target_g=None) -> None:
    """目標PFC（脂質・炭水化物）を明示的に保存する.

    カラムが無いDB（未マイグレーション）でも壊れないよう、
    存在するカラムだけを更新する。
    """
    cols = _table_cols("goal_profiles")
    sets = {}
    if fat_target_g is not None and "fat_target_g" in cols:
        sets["fat_target_g"] = float(fat_target_g)
    if carb_target_g is not None and "carb_target_g" in cols:
        sets["carb_target_g"] = float(carb_target_g)
    if not sets:
        logger.info("save_pfc_targets skipped (columns missing): %s", user_id)
        return
    with get_conn() as c:
        c.execute("INSERT INTO goal_profiles (user_id) VALUES (?)"
                  " ON CONFLICT(user_id) DO NOTHING", (user_id,))
        assigns = ", ".join(f"{k}=?" for k in sets)
        c.execute(f"UPDATE goal_profiles SET {assigns},"
                  " updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
                  (*sets.values(), user_id))


def pfc_targets(user_id: str, target_kcal: Optional[float] = None
                ) -> Optional[dict]:
    """その日の目標PFC(g)を返す。根拠が無ければ None.

    優先順位:
      - 明示設定（goal_profiles.fat_target_g / carb_target_g / protein_target_g）
      - 目安計算: たんぱく質 = 体重 × 1.6g、脂質 = 目標kcal × 25%、
        炭水化物 = 残りkcal（目標 − P − F）
    """
    p = get_profile(user_id) or {}
    kcal = target_kcal or p.get("calc_target_kcal")
    if not kcal:
        try:
            from app.services.db import get_goal
            kcal = get_goal(user_id, today_jst())
        except Exception:
            kcal = None
    kcal = float(kcal) if kcal else None
    cols = _table_cols("goal_profiles")
    weight = _latest_weight(user_id)
    protein = p.get("protein_target_g") if "protein_target_g" in cols else None
    if not protein and weight:
        protein = round(weight * PROTEIN_PER_KG, 1)
    fat = p.get("fat_target_g") if "fat_target_g" in cols else None
    carb = p.get("carb_target_g") if "carb_target_g" in cols else None
    if not fat and kcal:
        fat = round(kcal * FAT_RATIO / FAT_KCAL_PER_G, 1)
    if carb is None and kcal:
        rest = (kcal - (protein or 0) * PROTEIN_KCAL_PER_G
                - (fat or 0) * FAT_KCAL_PER_G)
        carb = round(max(rest, 0) / CARB_KCAL_PER_G, 1)
    out = {}
    if protein:
        out["protein_g"] = float(protein)
    if fat:
        out["fat_g"] = float(fat)
    if carb is not None:
        out["carb_g"] = float(carb)
    return out or None


# ---------- 計算 ----------

def _activity_avg_7d(user_id: str) -> Optional[float]:
    with get_conn() as c:
        r = c.execute(
            "SELECT AVG(total_kcal) AS a, COUNT(*) AS n FROM activity"
            " WHERE user_id=? AND date >= date(?, '-7 days')",
            (user_id, today_jst())).fetchone()
    d = dict(r) if r else {}
    return float(d["a"]) if d.get("n") else None


def _bmr(sex: str, weight: float, height: float, age: int) -> float:
    base = 10 * weight + 6.25 * height - 5 * age
    return base + 5 if sex == "male" else base - 161


def _latest_weight(user_id: str) -> Optional[float]:
    try:
        w = fetch_latest_weight(user_id)
        if w:
            return w["weight_kg"] if isinstance(w, dict) else w[1]
    except Exception:
        pass
    return None


# ---------- ウィザード ----------

def in_wizard(user_id: str) -> bool:
    return user_id in _pending


def start_wizard(user_id: str) -> TextSendMessage:
    _pending[user_id] = {"step": "mode"}
    msg = TextSendMessage(text=(
        "🎯 目的設定をはじめます。\n"
        "目的を選んでください（途中でやめるには「キャンセル」）"))
    msg.quick_reply = qr(
        msq("🐷 減量", "減量"), msq("🧂 減塩", "減塩"),
        msq("💪 筋肉増量", "筋肉増量"), msq("やめる", "キャンセル"))
    return msg


def _cancel(user_id: str) -> TextSendMessage:
    _pending.pop(user_id, None)
    return TextSendMessage(text="目的設定をキャンセルしました")


def _finish_weight(user_id: str, st: dict) -> TextSendMessage:
    cur, target, days = st["current"], st["target"], st["days"]
    maintenance = _activity_avg_7d(user_id)
    via = "活動量の実績"
    if maintenance is None:
        maintenance = _bmr(st["sex"], cur, st["height"], st["age"]) \
            * ACTIVITY_FACTOR
        via = "身体情報からの推定"
    deficit = (cur - target) * FAT_KCAL_PER_KG / days
    t_kcal = max(MIN_TARGET_KCAL, maintenance - deficit)
    st["calc"] = round(t_kcal, 1)
    st["step"] = "confirm"
    msg = TextSendMessage(text=(
        f"📋 計算結果\n"
        f"現在 {cur}kg → 目標 {target}kg（{-deficit:.0f}kcal/日の赤字）\n"
        f"消費カロリー見込み: {maintenance:.0f}kcal（{via}）\n\n"
        f"➡ 1日の目標摂取カロリー: 約{t_kcal:.0f}kcal\n\n"
        "この目標で設定しますか？"))
    msg.quick_reply = qr(msq("✅ 設定する", "目標確定"),
                         msq("やり直す", "目的設定"))
    return msg


def handle_step(user_id: str, text: str) -> Optional[TextSendMessage]:
    st = _pending.get(user_id)
    if st is None:
        return None
    text = text.strip()
    if text == "キャンセル":
        return _cancel(user_id)
    step = st["step"]

    if step == "mode":
        if text == "減量":
            if _latest_weight(user_id) is None:
                st["step"] = "w_current"
                return TextSendMessage(
                    text="まず現在の体重を kg で送ってください（例: 75）")
            st["current"] = _latest_weight(user_id)
            st["step"] = "w_target"
            return TextSendMessage(
                text=f"現在の体重は {st['current']}kg ですね。\n"
                     "目標体重を kg で送ってください（例: 65）")
        if text == "減塩":
            st["mode"] = "salt"
            st["step"] = "salt_confirm"
            msg = TextSendMessage(text=(
                f"🧂 減塩モード\n"
                f"一般的な目標は 1日 {DEFAULT_SALT_G}g です。この目標で進めますか？"))
            msg.quick_reply = qr(
                msq(f"{DEFAULT_SALT_G}gでOK", "塩分OK"),
                msq("数値を変える", "塩分変更"))
            return msg
        if text == "筋肉増量":
            w = _latest_weight(user_id)
            if w is None:
                st["mode"] = "muscle"
                st["step"] = "w_current"
                return TextSendMessage(
                    text="現在の体重を kg で送ってください（例: 75）")
            st["current"] = w
            return _finish_muscle(user_id, st)
        return start_wizard(user_id)

    if step == "w_current":
        try:
            st["current"] = float(text)
        except ValueError:
            return TextSendMessage(text="数字で送ってください（例: 75）")
        if st.get("mode") == "muscle":
            return _finish_muscle(user_id, st)
        st["step"] = "w_target"
        return TextSendMessage(
            text="目標体重を kg で送ってください（例: 65）")

    if step == "w_target":
        try:
            st["target"] = float(text)
        except ValueError:
            return TextSendMessage(text="数字で送ってください（例: 65）")
        if st["target"] >= st["current"]:
            return TextSendMessage(
                text="目標は現在の体重より小さい値にしてください。\n"
                     "目標体重を kg で送り直してください（例: 65）")
        st["step"] = "days"
        msg = TextSendMessage(text="期間を選んでください")
        msg.quick_reply = qr(
            msq("1ヶ月", "期間 30"), msq("2ヶ月", "期間 60"),
            msq("3ヶ月", "期間 90"), msq("半年", "期間 180"))
        return msg

    if step == "days":
        m = re.match(r"^期間\s*(\d+)$", text)
        v = m.group(1) if m else text
        try:
            st["days"] = int(v)
            if not (7 <= st["days"] <= 730):
                raise ValueError
        except ValueError:
            return TextSendMessage(
                text="期間はボタンから選ぶか、日数（7〜730）で送ってください")
        if _activity_avg_7d(user_id) is not None:
            return _finish_weight(user_id, st)
        st["step"] = "sex"
        msg = TextSendMessage(text=(
            "活動量のデータがまだ無いので、身体情報から消費カロリーを推定します。\n"
            "性別を教えてください"))
        msg.quick_reply = qr(msq("男性", "男性"), msq("女性", "女性"))
        return msg

    if step == "sex":
        if text not in ("男性", "女性"):
            msg = TextSendMessage(text="性別を選んでください")
            msg.quick_reply = qr(msq("男性", "男性"), msq("女性", "女性"))
            return msg
        st["sex"] = "male" if text == "男性" else "female"
        st["step"] = "age"
        return TextSendMessage(text="年齢を数字で送ってください（例: 35）")

    if step == "age":
        try:
            st["age"] = int(float(text))
        except ValueError:
            return TextSendMessage(text="数字で送ってください（例: 35）")
        st["step"] = "height"
        return TextSendMessage(text="身長を cm で送ってください（例: 172）")

    if step == "height":
        try:
            st["height"] = float(text)
        except ValueError:
            return TextSendMessage(text="数字で送ってください（例: 172）")
        return _finish_weight(user_id, st)

    if step == "salt_confirm":
        if text == "塩分OK":
            st["salt"] = DEFAULT_SALT_G
            return _finish_salt(user_id, st)
        if text == "塩分変更":
            st["step"] = "salt_custom"
            return TextSendMessage(
                text="1日の食塩目標を g で送ってください（例: 5.5）")
        msg = TextSendMessage(text="どちらかを選んでください")
        msg.quick_reply = qr(
            msq(f"{DEFAULT_SALT_G}gでOK", "塩分OK"),
            msq("数値を変える", "塩分変更"))
        return msg

    if step == "salt_custom":
        try:
            st["salt"] = float(text)
        except ValueError:
            return TextSendMessage(text="数字で送ってください（例: 5.5）")
        return _finish_salt(user_id, st)

    if step == "confirm":
        if text == "目標確定":
            save_profile(
                user_id, goal_mode="weight",
                target_weight_kg=st["target"], goal_days=st["days"],
                calc_target_kcal=st["calc"],
                sex=st.get("sex"), age=st.get("age"),
                height_cm=st.get("height"))
            set_goal(user_id=user_id, date=today_jst(),
                     target_kcal=st["calc"])
            _pending.pop(user_id, None)
            return TextSendMessage(text=(
                f"✅ 設定しました！\n"
                f"1日の目標摂取カロリー: {st['calc']:.0f}kcal\n"
                f"（{st['target']}kg / {st['days']}日）\n\n"
                "今後のコメントにもこの目標が反映されます。\n"
                "変更したいときはまた「目的設定」と送ってください"))
        return start_wizard(user_id)

    return start_wizard(user_id)


def _finish_salt(user_id: str, st: dict) -> TextSendMessage:
    save_profile(user_id, goal_mode="salt", salt_target_g=st["salt"])
    _pending.pop(user_id, None)
    return TextSendMessage(text=(
        f"✅ 設定しました！\n"
        f"食塩目標: {st['salt']}g/日\n\n"
        "今後のコメントに塩分の観点が反映されます"))


def _finish_muscle(user_id: str, st: dict) -> TextSendMessage:
    protein = round(st["current"] * PROTEIN_PER_KG, 1)
    save_profile(user_id, goal_mode="muscle", protein_target_g=protein)
    _pending.pop(user_id, None)
    return TextSendMessage(text=(
        f"✅ 設定しました！\n"
        f"体重 {st['current']}kg × {PROTEIN_PER_KG}g\n"
        f"➡ たんぱく質目標: {protein:.0f}g/日\n\n"
        "今後のコメントにたんぱく質の観点が反映されます"))
