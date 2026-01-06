import os
import sys
import time
import glob
import logging
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv

import yt_dlp
import google.generativeai as genai
from linebot import LineBotApi
from linebot.models import TextSendMessage

# ================= 1. 環境設定與初始化 =================

# 取得目前檔案所在的「絕對路徑」(讓程式不管在哪跑都找得到檔案)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "service.log")
HISTORY_FILE = os.path.join(BASE_DIR, "processed_videos.txt")

# 載入 .env 檔案
load_dotenv(os.path.join(BASE_DIR, ".env"))

# 讀取環境變數
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
LINE_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_USER_ID = os.getenv("LINE_USER_ID")
CHANNELS_STR = os.getenv("TARGET_CHANNELS", "")
TARGET_CHANNELS = [url.strip() for url in CHANNELS_STR.split(",") if url.strip()]

# 檢查 Key 是否存在
if not all([GOOGLE_API_KEY, LINE_TOKEN, LINE_USER_ID]):
    print("❌ 錯誤：請檢查 .env 檔案，API Key 缺失！")
    sys.exit(1)

# 設定 Google AI
genai.configure(api_key=GOOGLE_API_KEY)

# ================= 2. Log 系統設定 (專業版) =================
def setup_logger():
    logger = logging.getLogger("StockBot")
    logger.setLevel(logging.INFO)
    
    # 格式：時間 - 等級 - 訊息
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    
    # 檔案輪替：每個 1MB，最多留 5 個
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=5, encoding='utf-8')
    file_handler.setFormatter(formatter)
    
    # 螢幕輸出
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger

logger = setup_logger()

# ================= 3. 核心功能模組 =================

def load_history():
    """讀取已處理的影片 ID"""
    if not os.path.exists(HISTORY_FILE):
        return set()
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f)

def save_history(video_id):
    """儲存已處理的影片 ID"""
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(f"{video_id}\n")

def get_latest_video(channel_url):
    """檢查頻道最新影片"""
    logger.info(f"🔎 巡邏頻道: {channel_url}")
    ydl_opts = {'extract_flat': True, 'playlistend': 1, 'quiet': True, 'no_warnings': True}
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(channel_url, download=False)
            if 'entries' in info and info['entries']:
                v = info['entries'][0]
                return v['id'], v['title'], f"https://www.youtube.com/watch?v={v['id']}"
    except Exception as e:
        logger.error(f"❌ 讀取頻道失敗: {e}")
    return None, None, None

def download_audio(url):
    """下載音訊 (相容模式，不強制依賴 ffmpeg)"""
    logger.info(f"📥 開始下載: {url}")
    
    # 設定暫存檔路徑 (使用絕對路徑)
    output_prefix = os.path.join(BASE_DIR, "temp_audio")
    
    # 清理舊檔
    for f in glob.glob(f"{output_prefix}*"):
        try: os.remove(f)
        except: pass

    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio', 
        'outtmpl': f'{output_prefix}.%(ext)s', 
        'quiet': True,
        'no_warnings': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        
        # 尋找下載下來的檔案 (可能是 m4a 或 webm)
        found = glob.glob(f"{output_prefix}*")
        if found:
            return found[0]
        return None
    except Exception as e:
        logger.error(f"❌ 下載失敗: {e}")
        return None

def analyze_audio(audio_path, title):
    """上傳給 Gemini 進行分析"""
    logger.info(f"🧠 AI 分析中: {title}")
    
    # 判斷 MIME Type
    mime = "audio/webm" if audio_path.endswith(".webm") else "audio/mp4"
    
    myfile = genai.upload_file(audio_path, mime_type=mime)
    
    # 等待處理
    while myfile.state.name == "PROCESSING":
        time.sleep(2)
        myfile = genai.get_file(myfile.name)

    if myfile.state.name == "FAILED":
        raise ValueError("Audio processing failed on Google Server")

    # 使用最穩定的免費模型
    model = genai.GenerativeModel("gemini-flash-latest")
    
    promptVideo = f"""
    你是一位專業的分析師。影片標題為：「{title}」。
    請產出投資簡報 (繁體中文)：
    1. 【市場氣氛/題材訊息】：描述現在的市場氣氛或是本次題材的消息。
    2. 【重點摘要】：當前籌碼(外資/內資)看待方向，本次題材內容。
    3. 【焦點個股】：列出提到的股票代號/名稱，並給出「買進/觀望/賣出」建議。
    4. 【操作總結】：給用戶建議的操作方式。
    """
    
    result = model.generate_content([myfile, promptVideo])
    return result.text

def send_line(msg):
    """發送 LINE 通知"""
    try:
        api = LineBotApi(LINE_TOKEN)
        api.push_message(LINE_USER_ID, TextSendMessage(text=msg))
        logger.info("✅ LINE 通知發送成功")
    except Exception as e:
        logger.error(f"❌ LINE 發送失敗: {e}")

# ================= 4. 主迴圈 (Service Loop) =================
if __name__ == "__main__":
    logger.info("🤖 股票分析機器人已啟動 (Service Mode)")
    logger.info(f"📝 Log 檔位置: {LOG_FILE}")
    logger.info(f"🎯 監控頻道數: {len(TARGET_CHANNELS)}")

    while True:
        try:
            history = load_history()
            
            for channel in TARGET_CHANNELS:
                vid, title, url = get_latest_video(channel)
                
                if vid:
                    if vid in history:
                        logger.info(f"😴 [跳過] 已分析: {title}")
                    else:
                        logger.info(f"⚡ [新片] 發現新影片: {title}")
                        
                        audio = download_audio(url)
                        if audio:
                            try:
                                report = f"📢 新片快報：{title}\n{url}\n\n"
                                analysis = analyze_audio(audio, title)
                                report += analysis
                                
                                send_line(report)
                                save_history(vid)
                                logger.info(f"✅ 任務完成: {title}")
                            except Exception as e:
                                logger.error(f"❌ 處理過程錯誤: {e}")
                            finally:
                                # 確保刪除暫存檔
                                if os.path.exists(audio):
                                    os.remove(audio)
                
                time.sleep(1) # 頻道間稍微停頓
            
            logger.info("⏳ 待機 60 秒...")
            time.sleep(60)

        except KeyboardInterrupt:
            logger.warning("👋 程式手動停止")
            break
        except Exception as e:
            logger.critical(f"❌ 發生未預期錯誤: {e}")
            time.sleep(60)