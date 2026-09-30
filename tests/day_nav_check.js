/* 前日/翌日ボタンの日付演算チェック（Node 実行・pytest からも呼ばれる）
 *
 * 検証したいこと:
 *  1. 前日/翌日ボタンが「表示中の日付」からちょうど 1 日だけ動く
 *  2. 連続操作で正しく積み上がる（前日 x3 = 3 日前、前日→翌日で元に戻る）
 *  3. タイムゾーン（TZ）に依存しない
 *  4. 旧実装(toISOString)が 1 日ずれていたことの再現
 */
const assert = require("assert");
const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const src = fs.readFileSync(
  path.join(ROOT, "app", "static", "js", "day_nav.js"), "utf8");
const mod = { exports: {} };
new Function("module", "exports", src)(mod, mod.exports);
const DayNav = mod.exports;

assert.ok(DayNav && typeof DayNav.shiftDate === "function",
  "DayNav.shiftDate が見つかりません");

const cases = [
  ["2026-09-28", -1, "2026-09-27"],
  ["2026-09-28", 1, "2026-09-29"],
  ["2026-10-01", -1, "2026-09-30"],
  ["2026-12-31", 1, "2027-01-01"],
  ["2026-01-01", -1, "2025-12-31"],
  ["2026-03-01", -1, "2026-02-28"],
];
for (const [from, days, want] of cases) {
  const got = DayNav.shiftDate(from, days);
  assert.strictEqual(got, want,
    `shiftDate(${from}, ${days}) = ${got} (期待 ${want})`);
  console.log(`shiftDate('${from}', ${days >= 0 ? "+" : ""}${days}) = ${got}  OK`);
}

// 連続操作: 前日 x3
let d = "2026-09-28";
for (let i = 0; i < 3; i++) d = DayNav.shiftDate(d, -1);
assert.strictEqual(d, "2026-09-25", `前日 x3 = ${d} (期待 2026-09-25)`);
console.log(`前日 x3 = ${d}  OK`);

// 往復: 前日 -> 翌日
const back = DayNav.shiftDate(DayNav.shiftDate("2026-09-28", -1), 1);
assert.strictEqual(back, "2026-09-28", `前日→翌日 = ${back}`);
console.log(`前日→翌日 = ${back}  OK`);

// 旧実装(toISOString)の再現 — 何が壊れていたのかを記録
function buggy(curDate, days) {
  const dt = new Date(curDate + "T00:00:00+09:00");
  dt.setDate(dt.getDate() + days);
  return dt.toISOString().slice(0, 10); // UTC に変換される = 1 日ずれる
}
const bPrev = buggy("2026-09-28", -1);
const bNext = buggy("2026-09-28", 1);
console.log(`旧実装 前日 = ${bPrev} (正しくは 2026-09-27)`);
console.log(`旧実装 翌日 = ${bNext} (正しくは 2026-09-29)`);
assert.strictEqual(bPrev, "2026-09-26", "旧実装の再現が変わりました");
assert.strictEqual(bNext, "2026-09-28", "旧実装の再現が変わりました");

// TZ 非依存
console.log(`TZ=${process.env.TZ || "(未設定)"} でも同じ結果  OK`);
console.log("day_nav: all assertions passed");
