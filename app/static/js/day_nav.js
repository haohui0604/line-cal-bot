/* 日別ビューの日付ナビゲーション（ブラウザ / Node 両対応）
 *
 * 重要: toISOString() は UTC に変換するため、
 *   new Date("2026-09-28T00:00:00+09:00").toISOString() -> "2026-09-27T15:00:00Z"
 * となり、日付部分が 1 日ずれる。
 * これが「前日ボタンが 2 日戻る」「翌日ボタンが動かない」の原因だった。
 * 日付の組み立ては必ずローカル時刻で行うこと。
 */
(function (global) {
  function pad(n) { return String(n).padStart(2, "0"); }

  /** Date -> "YYYY-MM-DD"（ローカル時刻基準。toISOString は使わない） */
  function fmtLocal(d) {
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  }

  /** "YYYY-MM-DD" -> ローカル正午の Date（深夜0時だと夏時間等の境界でずれるため正午） */
  function parseLocal(s) {
    const p = String(s || "").split("-");
    if (p.length !== 3) throw new Error("invalid date: " + s);
    return new Date(Number(p[0]), Number(p[1]) - 1, Number(p[2]), 12, 0, 0);
  }

  /** 表示中の日付 curDate から days 日だけ移動した日付文字列を返す */
  function shiftDate(curDate, days) {
    const d = parseLocal(curDate);
    d.setDate(d.getDate() + days);
    return fmtLocal(d);
  }

  /** 今日（ローカル基準） */
  function todayLocal() { return fmtLocal(new Date()); }

  /** 前日（ローカル基準）— 日別ビューの初期表示に使う */
  function yesterdayLocal() {
    const d = new Date();
    d.setDate(d.getDate() - 1);
    return fmtLocal(d);
  }

  const api = {
    shiftDate: shiftDate,
    parseLocal: parseLocal,
    fmtLocal: fmtLocal,
    todayLocal: todayLocal,
    yesterdayLocal: yesterdayLocal,
  };
  global.DayNav = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
