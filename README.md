# 派工單 Bot（Gemini 版）

## 環境變數設定（Railway）

| 變數名稱 | 說明 | 範例 |
|---------|------|------|
| LINE_CHANNEL_SECRET | LINE Channel Secret | f328223f... |
| LINE_CHANNEL_ACCESS_TOKEN | LINE Channel Access Token | w5MQhMu... |
| GEMINI_API_KEY | Google Gemini API Key | AIzaSyC3... |
| GOOGLE_DRIVE_FILE_ID | Google 試算表 ID | 1cEQMiY8... |
| GOOGLE_SERVICE_ACCOUNT_JSON | 服務帳戶 JSON 內容（整個貼上）| {...} |

## Webhook URL
部署後填入 LINE Developers Console：
```
https://你的Railway網址/callback
```
