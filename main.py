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
GOOGLE_DRIVE_FILE_ID = os.environ.get('GOOGLE_DRIVE_FILE_ID')
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')

handler = WebhookHandler(LINE_CHANNEL_SECRET)
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)

VISION_API_URL = "https://vision.googleapis.com/v1/images:annotate"

def get_credentials():
    service_account_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    scopes = [
        'https://www.googleapis.com/auth/cloud-vision',
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive'
    ]
    return Credentials.from_service_account_info(service_account_info, scopes=scopes)

def get_gsheet():
    creds = get_credentials()
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

def ocr_with_vision(image_data):
    """使用 Google Cloud Vision API 進行 OCR"""
    creds = get_credentials()
    
    # 取得 access token
    import google.auth.transport.requests
    request_obj = google.auth.transport.requests.Request()
    creds.refresh(request_obj)
    
    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "requests": [{
            "image": {"content": image_data},
            "features": [{"type": "DOCUMENT_TEXT_DETECTION"}]
        }]
    }
    
    resp = requests.post(VISION_API_URL, json=payload, headers=headers, timeout=30)
    result = resp.json()
    print(f"Vision API response: {json.dumps(result, ensure_ascii=False)[:500]}")
    
    if 'responses' not in result or not result['responses']:
        raise Exception("Vision API 無回應")
    
    response = result['responses'][0]
    if 'error' in response:
        raise Exception(f"Vision API 錯誤: {response['error'].get('message', '未知錯誤')}")
    
    if 'fullTextAnnotation' not in response:
        return ""
    
    return response['fullTextAnnotation']['text']

def parse_text_to_data(text):
    """將 OCR 文字解析為派工單資料"""
    print(f"OCR 文字：{text}")
    
    data = {
        "工程名稱": "",
        "工程地點": "",
        "派工日期": "",
        "司機姓名": "",
        "車牌號碼": "",
        "工作時數": "",
        "工作內容": "",
        "簽名確認": "無",
        "備註": ""
    }
    
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    full_text = ' '.join(lines)
    
    import re
    
    # 業主（工程名稱）
    for pattern in [r'業主[：:]\s*(.+?)(?:\s|地點|$)', r'業主\s+(\S+)']:
        m = re.search(pattern, full_text)
        if m:
            data['工程名稱'] = m.group(1).strip()
            break
    
    # 地點
    for pattern in [r'地點[：:]\s*(.+?)(?:\s{2,}|機型|$)', r'地點\s+(\S+)']:
        m = re.search(pattern, full_text)
        if m:
            data['工程地點'] = m.group(1).strip()
            break
    
    # 日期
    for pattern in [r'(\d{2,3})[年/]\s*(\d{1,2})[月/]\s*(\d{1,2})', r'(\d{4})[/-](\d{1,2})[/-](\d{1,2})']:
        m = re.search(pattern, full_text)
        if m:
            y, mo, d = m.group(1), m.group(2), m.group(3)
            if len(y) == 3:  # 民國年
                y = str(int(y) + 1911)
            data['派工日期'] = f"{y}/{mo.zfill(2)}/{d.zfill(2)}"
            break
    
    # 車牌
    m = re.search(r'車號[：:]?\s*([A-Z0-9]{2,4}[-‐–]\d{2,4})', full_text)
    if not m:
        m = re.search(r'([A-Z]{2,3}[-‐–]\d{4}|\d{2,4}[-‐–][A-Z]{2,3}|[A-Z0-9]{3,4}[-‐]\d{3,4})', full_text)
    if m:
        data['車牌號碼'] = m.group(1).strip()
    
    # 工作時數
    m = re.search(r'合計\s*(\d+)\s*時', full_text)
    if not m:
        m = re.search(r'(\d+)\s*時', full_text)
    if m:
        data['工作時數'] = f"{m.group(1)}時"
    
    # 工作內容
    m = re.search(r'工作內容[：:]?\s*(.+?)(?:備註|$)', full_text)
    if m:
        data['工作內容'] = m.group(1).strip()
    
    # 簽名確認（有業主簽名和司機簽名就算有）
    if '業主簽名' in full_text or '簽名' in full_text:
        data['簽名確認'] = '有'
    
    return data

def check_missing(data):
    required = ['工程名稱', '派工日期', '車牌號碼', '工作時數']
    return [f for f in required if not data.get(f)]

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
        ocr_text = ocr_with_vision(image_data)

        if not ocr_text:
            push_msg(target_id, '⚠️ 無法辨識圖片文字，請確認圖片清晰後重新上傳。')
            return

        data = parse_text_to_data(ocr_text)
        missing = check_missing(data)

        if missing:
            push_msg(target_id, '❌ 以下欄位無法辨識：\n' + '\n'.join(f'  • {f}' for f in missing) + '\n\n請補齊後重新上傳。')
            return

        ws = get_gsheet()
        init_sheet(ws)
        ws.append_row([
            datetime.now().strftime('%Y/%m/%d %H:%M'),
            data.get('工程名稱',''), data.get('工程地點',''),
            data.get('派工日期',''), data.get('司機姓名',''),
            data.get('車牌號碼',''), data.get('工作時數',''),
            data.get('工作內容',''), data.get('簽名確認',''),
            data.get('備註',''), '已登錄'
        ])

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
