-- 009: コメントを「日レポ（日付）」単位に寄せる
--
-- 背景: Phase 1 のコメントは entry_id（個別の食事記録）に紐づくことがあり、
--       どの日の話か特定できないものがあった。
--       日別ビューでは target_date でコメントを引くため、
--       日付の無い既存コメントに日付を与えて移行する。

-- 1) entry_id がある場合は、その記録の日付を採用
UPDATE comments
   SET target_date = (
         SELECT e.date FROM entries e WHERE e.id = comments.entry_id)
 WHERE (target_date IS NULL OR target_date = '')
   AND entry_id IS NOT NULL
   AND EXISTS (SELECT 1 FROM entries e WHERE e.id = comments.entry_id);

-- 2) それでも決まらない場合は投稿日（created_at）の日付を採用
UPDATE comments
   SET target_date = date(created_at)
 WHERE (target_date IS NULL OR target_date = '')
   AND created_at IS NOT NULL;

-- 3) 日付単位の検索を高速化
CREATE INDEX IF NOT EXISTS idx_comments_user_date
    ON comments (user_id, target_date);
