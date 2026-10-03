"""LINEのIDトークン検証まわり（400 → 401 → ログイン無限ループ）を修正するパッチ.

対象:
  app/auth.py                    : 検証失敗時に LINE の error/error_description をログに残す
  app/web/member.py              : 401 の理由をレスポンスに載せる（期限切れ/不一致の切り分け）
  app/templates/member_home.html : トークンを毎回取得 / 401時は logout → login を「1回だけ」
使い方:
  python3 patch_idtoken.py                    # リポジトリの3ファイルを修正
  python3 patch_idtoken.py --html X.html      # HTMLだけ当てて X.js を書き出す（構文確認用）
"""
import sys
from pathlib import Path


def func_span(s, header):
    """JSの関数を波括弧の対応で切り出す（コメント差分に強い）."""
    i = s.index(header)
    j = s.index("{", i + len(header) - 1)
    depth, k = 0, j
    while k < len(s):
        if s[k] == "{":
            depth += 1
        elif s[k] == "}":
            depth -= 1
            if depth == 0:
                return i, k + 1
        k += 1
    raise ValueError("brace mismatch: " + header)


# ---------------- app/auth.py ----------------
NEW_VERIFY = '''class IdTokenError(Exception):
    """LINEのIDトークン検証に失敗した（理由つき）."""

    def __init__(self, status: int, error: str = "", description: str = ""):
        self.status = status
        self.error = error
        self.description = description
        super().__init__(f"verify {status}: {error} / {description}")


def verify_id_token(id_token: str) -> dict:
    """id_token を検証し {"sub": line_user_id, "name": ..., "picture": ...} を返す.

    LIFFのIDトークンも同じログインチャンネルならこの関数で検証できる。
    失敗時は LINE が返す error / error_description を保持して IdTokenError を投げる。
    400 の理由（IdToken expired / Invalid IdToken Audience 等）を必ずログに残し、
    「再ログインで直るのか、設定ミスなのか」を切り分けられるようにする。
    """
    resp = httpx.post(VERIFY_URL, data={
        "id_token": id_token,
        "client_id": settings.LINE_LOGIN_CHANNEL_ID,
    }, timeout=10)
    if resp.status_code != 200:
        try:
            body = resp.json()
        except Exception:
            body = {"error": "non_json",
                    "error_description": (resp.text or "")[:300]}
        error = str(body.get("error") or "")
        desc = str(body.get("error_description") or "")
        logger.error("LINE verify failed: status=%s client_id=%s error=%s "
                     "desc=%s token_len=%s",
                     resp.status_code, settings.LINE_LOGIN_CHANNEL_ID,
                     error, desc, len(id_token or ""))
        raise IdTokenError(resp.status_code, error, desc)
    return resp.json()
'''


def patch_auth(p: Path):
    s = p.read_text(encoding="utf-8")
    if "class IdTokenError" in s:
        return "skip(適用済み)"
    i = s.index("def verify_id_token(")
    j = s.index("# ---- セッションCookie")
    p.write_text(s[:i] + NEW_VERIFY + "\n\n" + s[j:], encoding="utf-8")
    return "ok"


# ---------------- app/web/member.py ----------------
OLD_UID = '''def _verify_uid(id_token: str) -> str:
    """LIFFのIDトークンを検証して line_user_id を返す."""
    try:
        claims = auth.verify_id_token(id_token)
    except Exception:
        logger.warning("liff id_token verify failed", exc_info=True)
        raise HTTPException(status_code=401, detail="認証に失敗しました")
    uid = claims["sub"]
    gym_db.upsert_user(uid, claims.get("name"), claims.get("picture"))
    return uid'''

NEW_UID = '''def _verify_uid(id_token: str) -> str:
    """LIFFのIDトークンを検証して line_user_id を返す.

    失敗理由をレスポンスにも載せる。期限切れ（expired）は再ログインで直るが、
    audience 不一致などの設定ミスは何度ログインしても直らないため、
    フロントとログの両方で区別できるようにする。
    """
    try:
        claims = auth.verify_id_token(id_token)
    except Exception as e:
        error = getattr(e, "error", "") or type(e).__name__
        desc = getattr(e, "description", "")
        logger.warning("liff id_token verify failed: error=%s desc=%s",
                       error, desc)
        hint = "expired" if "expir" in desc.lower() else "invalid"
        raise HTTPException(
            status_code=401,
            detail=f"認証に失敗しました（{hint} / {error}）")
    uid = claims["sub"]
    gym_db.upsert_user(uid, claims.get("name"), claims.get("picture"))
    return uid'''


def patch_member(p: Path):
    s = p.read_text(encoding="utf-8")
    if 'hint = "expired"' in s:
        return "skip(適用済み)"
    if OLD_UID not in s:
        return "NG(_verify_uid が見つからない)"
    p.write_text(s.replace(OLD_UID, NEW_UID, 1), encoding="utf-8")
    return "ok"


# ---------------- member_home.html ----------------
NEW_LOGIN = '''let _reloginTried = false;                          // 同一ページ内の多重ループ防止
const _GUARD_KEY = "liff_relogin_at_" + LIFF_ID;    // ページ跨ぎのループ防止

// LIFF は古い ID トークンをキャッシュしたまま返すため、毎回取り直す
function freshToken(){
  try { return (liff.getIDToken && liff.getIDToken()) || null; } catch(_) { return null; }
}

// 期限切れトークンを捨てるため、必ず logout → login の順で「1回だけ」再ログイン。
// logout を挟まないと古いトークンのまま戻ってきて、401 → login を無限に繰り返す。
function reloginOnce(){
  if (_reloginTried) return false;
  _reloginTried = true;
  let last = 0;
  try { last = Number(sessionStorage.getItem(_GUARD_KEY) || 0); } catch(_) {}
  if (last && Date.now() - last < 60000) {           // 1分以内の再入はループと判定
    showErr("ログインが繰り返し失敗しています。\\n"
      + "LINEアプリのトークから開き直すか、少し時間をおいてから試してください。");
    return false;
  }
  try { sessionStorage.setItem(_GUARD_KEY, String(Date.now())); } catch(_) {}
  try { liff.logout(); } catch(_) {}
  liff.login({redirectUri: location.origin + location.pathname});
  return true;
}

async function ensureLogin(){
  idToken = freshToken();
  if (idToken) return true;
  if (!liff.isLoggedIn()) { reloginOnce(); return false; }
  showErr("ログイン情報を取得できませんでした。\\n"
    + "LINEアプリのトークから開き直すか、下の「再読み込み」を押してください。\\n"
    + "(isInClient=" + liff.isInClient() + " / loggedIn=" + liff.isLoggedIn() + ")");
  return false;
}'''

NEW_401 = '''if(res.status === 401){
    let _d = ""; try { _d = (await res.json()).detail || ""; } catch(_) {}
    reloginOnce();     // 古いIDトークンを捨てて、1回だけ再ログイン
    throw new Error("401 認証エラー" + (_d ? "（" + _d + "）" : "") +
                    "。再読み込みしてください");
  }'''


def patch_html_text(s: str):
    """会員画面のトークン取得と401処理を差し替える（本番版/Phase9版の両方に適用可）."""
    if "reloginOnce" in s:
        return s, "skip(適用済み)"
    # 1) ensureLogin 一式（_reloginTried 宣言 〜 関数末尾）
    i = s.index("let _reloginTried = false;")
    j = func_span(s, "async function ensureLogin(){")[1]
    s = s[:i] + NEW_LOGIN + s[j:]
    # 2) api(): 毎回トークンを取り直す（古いIDトークンを掴まない）
    hdr = "async function api(path, body){"
    a, b = func_span(s, hdr)
    body = s[a:b]
    assert "{id_token: idToken}" in body, "api() に id_token 参照が無い"
    body = body.replace(
        hdr,
        hdr + "\n  idToken = freshToken();   // LIFFは古いトークンを返すことがあるので毎回取り直す\n"
              "  if (!idToken) { reloginOnce(); "
              "throw new Error(\"認証情報を取得できませんでした\"); }", 1)
    # 3) 401 は logout→login を1回だけ
    d, e = func_span(body, "if(res.status === 401){")
    body = body[:d] + NEW_401 + body[e:]
    # 4) 成功したらループ防止キーを消す
    body = body.replace("  return res.json();\n}",
                        "  try { sessionStorage.removeItem(_GUARD_KEY); } catch(_) {}\n"
                        "  return res.json();\n}", 1)
    s = s[:a] + body + s[b:]
    assert "freshToken()" in s and "liff.logout()" in s
    return s, "ok"


def patch_html(p: Path):
    s = p.read_text(encoding="utf-8")
    s2, msg = patch_html_text(s)
    if msg.startswith("skip"):
        return msg
    p.write_text(s2, encoding="utf-8")
    return msg


def main():
    if "--html" in sys.argv:
        p = Path(sys.argv[sys.argv.index("--html") + 1])
        print("HTML:", patch_html(p))
        return
    print("app/auth.py :", patch_auth(Path("app/auth.py")))
    print("app/web/member.py :", patch_member(Path("app/web/member.py")))
    print("app/templates/member_home.html :",
          patch_html(Path("app/templates/member_home.html")))


if __name__ == "__main__":
    main()
