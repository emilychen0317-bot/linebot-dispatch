import os
import json
import base64
import requests
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, PushMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, ImageMessageContent
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
GOOGLE_DRIVE_FILE_ID = os.environ.get('GOOGLE_DRIVE_FILE_ID')
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')

handler = WebhookHandler(LINE_CHANNEL_SECRET)
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"

def get_gsheet():
    service_account_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    scopes = ['https://www.googleapis.com/auth/spreadsheets','https://www.googleapis.com/auth/drive']
    creds = Credentials.from_service_account_info(service_account_info, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(GOOGLE_DRIVE_FILE_ID).sheet1

def init_sheet(ws):
    if not ws.row_values(1):
        ws.append_row(['上傳時間','工程名稱','工程地點','派工日期','司機姓名','車牌號碼','工作時數','工作內容','簽名確認','備註','狀態'])

def download_image(message_id):
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    resp = requests.get(url, headers=headers, timeout=30)
    return base64.b64encode(resp.content).decode('utf-8')

def analyze_image_with_gemini(image_data):
    prompt = '請仔細辨識這張工程派工單圖片，提取以下資訊並以 JSON 格式回傳：{"工程名稱":"","工程地點":"","派工日期":"","司機姓名":"","車牌號碼":"","工作時數":"","工作內容":"","簽名確認":"有/無","備註":""}。只回傳JSON不要其他文字。'
    payload = {
        "contents":[{
            "parts":[
                {"text": prompt},
                {"inline_data": {"mime_type": "image/jpeg", "data": image_data}}
            ]
        }],
        "generationConfig": {"temperature": 0.1}
    }
    resp = requests.post(f"{GEMINI_API_URL}?key={GEMINI_API_KEY}", json=payload, timeout=60)
    result = resp.json()
    print(f"Gemini response status: {resp.status_code}")
    print(f"Gemini response: {json.dumps(result, ensure_ascii=False)[:500]}")
    
    if 'candidates' not in result:
        error_msg = result.get('error', {}).get('message', '未知錯誤')
        raise Exception(f"Gemini API 錯誤: {error_msg}")
    
    return result["candidates"][0]["content"]["parts"][0]["text"]

def parse_data(text):
    try:
        clean = text.strip()
        if '```' in clean:
            clean = clean.split('```')[1]
            if clean.startswith('json'): clean = clean[4:]
        return json.loads(clean.strip())
    except:
        return None

def push_msg(target_id, text):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).push_message(PushMessageRequest(to=target_id, messages=[TextMessage(text=text)]))

def reply_msg(reply_token, text):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(ReplyMessageRequest(reply_token=reply_token, messages=[TextMessage(text=text)]))

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event):
    try:
        reply_msg(event.reply_token, '📋 收到派工單，辨識中，請稍候...')
        target_id = getattr(event.source, 'group_id', None) or event.source.user_id
        image_data = download_image(event.message.id)
        gemini_text = analyze_image_with_gemini(image_data)
        data = parse_data(gemini_text)

        if not data:
            push_msg(target_id, f'⚠️ 無法解析辨識結果，請重新上傳清晰圖片。\n原始結果：{gemini_text[:200]}')
            return

        missing = [f for f in ['工程名稱','派工日期','司機姓名','車牌號碼','工作時數','簽名確認'] if not data.get(f) or data[f] in ('','無')]
        if missing:
            push_msg(target_id, '❌ 資料不完整，缺少：\n' + '\n'.join(f'  • {f}' for f in missing) + '\n\n請補齊後重新上傳。')
            return

        ws = get_gsheet()
        init_sheet(ws)
        ws.append_row([datetime.now().strftime('%Y/%m/%d %H:%M'), data.get('工程名稱',''), data.get('工程地點',''), data.get('派工日期',''), data.get('司機姓名',''), data.get('車牌號碼',''), data.get('工作時數',''), data.get('工作內容',''), data.get('簽名確認',''), data.get('備註',''), '已登錄'])

        push_msg(target_id, f"✅ 派工單登錄成功！\n\n📌 工程名稱：{data.get('工程名稱','')}\n📍 地點：{data.get('工程地點','')}\n📅 日期：{data.get('派工日期','')}\n👷 司機：{data.get('司機姓名','')}\n🚛 車牌：{data.get('車牌號碼','')}\n⏱️ 時數：{data.get('工作時數','')}\n✍️ 簽名：{data.get('簽名確認','')}\n\n資料已同步至 Google Drive ✓")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        target_id = getattr(event.source, 'group_id', None) or event.source.user_id
        push_msg(target_id, f'⚠️ 系統錯誤：{str(e)[:100]}')

@app.route("/", methods=['GET'])
def health():
    return '派工單 Bot 運作中 ✅'

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
