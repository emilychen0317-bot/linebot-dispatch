import os
import json
import base64
import requests
import gspread
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, ImageMessage, TextSendMessage
from google.oauth2.service_account import Credentials
from datetime import datetime
import google.generativeai as genai

app = Flask(__name__)

# 環境變數
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
GOOGLE_DRIVE_FILE_ID = os.environ.get('GOOGLE_DRIVE_FILE_ID')
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 設定 Gemini
genai.configure(api_key=GEMINI_API_KEY)

def get_gsheet():
    """取得 Google Sheets 連線"""
    service_account_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    scopes = [
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive'
    ]
    creds = Credentials.from_service_account_info(service_account_info, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(GOOGLE_DRIVE_FILE_ID)
    worksheet = sh.sheet1
    return worksheet

def init_sheet(worksheet):
    """初始化試算表標題列"""
    headers = worksheet.row_values(1)
    if not headers:
        worksheet.append_row([
            '上傳時間', '工程名稱', '工程地點', '派工日期',
            '司機姓名', '車牌號碼', '工作時數', '工作內容',
            '簽名確認', '備註', '狀態'
        ])

def analyze_image_with_gemini(image_data):
    """使用 Gemini 分析派工單圖片"""
    model = genai.GenerativeModel('gemini-1.5-flash')
    
    prompt = """請仔細辨識這張工程派工單圖片，提取以下資訊並以 JSON 格式回傳：
{
  "工程名稱": "",
  "工程地點": "",
  "派工日期": "",
  "司機姓名": "",
  "車牌號碼": "",
  "工作時數": "",
  "工作內容": "",
  "簽名確認": "有/無",
  "備註": ""
}

注意：
- 如果某欄位看不清楚或不存在，請填入空字串
- 日期格式請統一為 YYYY/MM/DD
- 只回傳 JSON，不要其他說明文字"""

    image_part = {
        "mime_type": "image/jpeg",
        "data": image_data
    }
    
    response = model.generate_content([prompt, image_part])
    return response.text

def parse_dispatch_data(gemini_response):
    """解析 Gemini 回傳的 JSON 資料"""
    try:
        # 清理回傳內容（移除 markdown 格式）
        clean = gemini_response.strip()
        if clean.startswith('```'):
            clean = clean.split('```')[1]
            if clean.startswith('json'):
                clean = clean[4:]
        clean = clean.strip()
        return json.loads(clean)
    except:
        return None

def check_missing_fields(data):
    """檢查缺失欄位"""
    required_fields = {
        '工程名稱': '工程名稱',
        '派工日期': '派工日期',
        '司機姓名': '司機姓名',
        '車牌號碼': '車牌號碼',
        '工作時數': '工作時數',
        '簽名確認': '簽名確認'
    }
    missing = []
    for field, label in required_fields.items():
        if not data.get(field) or data[field] == '無':
            missing.append(label)
    return missing

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    
    return 'OK'

@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    """處理圖片訊息"""
    try:
        # 下載圖片
        message_content = line_bot_api.get_message_content(event.message.id)
        image_data = base64.b64encode(message_content.content).decode('utf-8')
        
        # 回覆處理中訊息
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text='📋 收到派工單，辨識中，請稍候...')
        )
        
        # 用 Gemini 分析圖片
        gemini_response = analyze_image_with_gemini(image_data)
        data = parse_dispatch_data(gemini_response)
        
        if not data:
            line_bot_api.push_message(
                event.source.group_id if hasattr(event.source, 'group_id') else event.source.user_id,
                TextSendMessage(text='⚠️ 無法辨識派工單內容，請確認圖片是否清晰，重新上傳。')
            )
            return
        
        # 檢查缺失欄位
        missing = check_missing_fields(data)
        target_id = event.source.group_id if hasattr(event.source, 'group_id') else event.source.user_id
        
        if missing:
            missing_text = '\n'.join([f'  • {f}' for f in missing])
            msg = f'❌ 派工單資料不完整，以下欄位缺失：\n{missing_text}\n\n請補齊後重新上傳。'
            line_bot_api.push_message(target_id, TextSendMessage(text=msg))
            return
        
        # 寫入 Google Sheets
        worksheet = get_gsheet()
        init_sheet(worksheet)
        
        now = datetime.now().strftime('%Y/%m/%d %H:%M')
        row = [
            now,
            data.get('工程名稱', ''),
            data.get('工程地點', ''),
            data.get('派工日期', ''),
            data.get('司機姓名', ''),
            data.get('車牌號碼', ''),
            data.get('工作時數', ''),
            data.get('工作內容', ''),
            data.get('簽名確認', ''),
            data.get('備註', ''),
            '已登錄'
        ]
        worksheet.append_row(row)
        
        # 回傳成功訊息
        success_msg = f"""✅ 派工單登錄成功！

📌 工程名稱：{data.get('工程名稱', '')}
📍 工程地點：{data.get('工程地點', '')}
📅 派工日期：{data.get('派工日期', '')}
👷 司機姓名：{data.get('司機姓名', '')}
🚛 車牌號碼：{data.get('車牌號碼', '')}
⏱️ 工作時數：{data.get('工作時數', '')}
✍️ 簽名確認：{data.get('簽名確認', '')}

資料已同步至 Google Drive ✓"""
        
        line_bot_api.push_message(target_id, TextSendMessage(text=success_msg))
        
    except Exception as e:
        print(f"Error: {e}")
        target_id = event.source.group_id if hasattr(event.source, 'group_id') else event.source.user_id
        line_bot_api.push_message(
            target_id,
            TextSendMessage(text=f'⚠️ 系統發生錯誤，請稍後再試。')
        )

@app.route("/", methods=['GET'])
def health():
    return '派工單 Bot 運作中 ✅'

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
