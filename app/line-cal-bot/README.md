# LINE カロリー管理 Bot

本スレッドで運用していた"OCRラベル/ユーザー報告/Garmin" 3ソース＋確定/推定フラグ付きカロリー管理を、LINE Messaging API webhook bot として製品化したもの。デプロイ運用は $0/月。

## アーキテクチャ

```
ユーザ (LINE)
  │  画像 / テキスト
  ▼
LINE Platform
  │  Webhook (X-Line-Signature)
  ▼
FastAPI  ┌─▶ image_handler ─▶ Gemini 2.0 Flash (OCR)
         └─▶ text_handler  ─▶ ローカル解析
                            ▼
                          SQLite (entries / activity / weight_logs)
                            ▼
                         Meal Summary (Flex Message)
                            ▼
ユーザ (LINE) へ返信
```

## セットアップ手順

### 1. LINE 公式アカウント／Messaging API チャネル開設
1. [LINE Official Account Manager](https://www.linebiz.com/jp/service/line-official-account/) にログイン
2. アカウント作成 → 設定 → Messaging API → チャネル作成
3. `Channel access token`（長期）と `Channel secret` を発行
4. Webhook URL にデプロイ後の URL を設定（例: `https://your-app.onrender.com/callback`）
5. **応答メッセージ OFF**／**webhook ON** に切替

### 2. Gemini API キー取得
- [Google AI Studio](https://aistudio.google.com/app/apikey) で API キー発行（無料）

### 3. デプロイ（Render）
```bash
# GitHub に push → Render で New → Web Service → GitHub repo 選択
# 環境変数：
#   LINE_CHANNEL_ACCESS_TOKEN
#   LINE_CHANNEL_SECRET
#   GEMINI_API_KEY
#   DB_PATH = /opt/render/project/src/data/cal.db
```

### 4. ローカル開発
```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
# ngrok で 8000 をトンネリング → LINE Webhook URL に設定
```

## 使い方（LINE）

### テキスト入力例
```
9/9 朝 Milimホエイ25g 豆乳200
9/10 昼 ルーローハン81g 卵 サラダ
集計        # 今日のサマリ
履歴        # 直近7日
グラフ      # PNG画像を送信
```

### 画像入力
- 栄養成分ラベル／Garminスクショ を送るだけ → 自動 OCR → 確認プロンプト
- 「OK」で確定 / 「修正: ○○」で上書き

## ソース認定（精度管理）

| ソース種別 | 信頼度 | 例 |
|---|---|---|
| `ocr_label` | confirmed | 画像から成分値を直接抽出 |
| `garmin_ocr` | confirmed | Garminスクショから消費kcal/体重 |
| `user_report` | estimated | 1行テキスト手入力 |
| `ai_estimate` | placeholder | 推測値（要確認） |

## データモデル（SQLite）

`migrations/001_init.sql` に3テーブル定義：
- `entries`: 食事ログ（P/F/C/食塩/分量）
- `activity`: Garmin日次消費
- `weight_logs`: 体重＋体組成

## テスト

```bash
pytest tests/
```

アプリ内の `calorie_calc.py` は本スレッドの検算ロジック（◯kcal/P◯g/F◯g/食塩◯g）を移植済み。

## 注意事項

- LINE Free プランは月200メッセージ。1日3食×3ソース＋日次サマリ=約10通/日なので余裕（1ユーザー前提）
- 8ユーザー以上同時の場合は Light プラン（5,000円/月、5,000通）への切替を検討
- サーバースリープ（Render Free）後の初回レスポンスが遅い場合は cron-job で 5分間隔の ping を設定
