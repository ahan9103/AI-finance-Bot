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

# ================= 1. 環境設定 =================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "service.log")
HISTORY_FILE = os.path.join(BASE_DIR, "processed_videos.txt")

# 載入 .env
load_dotenv(os.path.join(BASE_DIR, ".env"))

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
LINE_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_USER_ID = os.getenv("LINE_USER_ID")
CHANNELS_STR = os.getenv("TARGET_CHANNELS", "")
TARGET_CHANNELS = [url.strip() for url in CHANNELS_STR.split(",") if url.strip()]

if not all([GOOGLE_API_KEY, LINE_TOKEN, LINE_USER_ID]):
    print("❌ 錯誤：請檢查 .env 檔案，API Key 缺失！")
    sys.exit(1)

genai.configure(api_key=GOOGLE_API_KEY)

# ================= 2. Log 系統 =================
def setup_logger():
    logger = logging.getLogger("StockBot")
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=5, encoding='utf-8')
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger

logger = setup_logger()

# ================= 3. 核心功能 =================

def load_history():
    if not os.path.exists(HISTORY_FILE): return set()
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f)

def save_history(video_id):
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(f"{video_id}\n")

def get_latest_video(channel_url):
    logger.info(f"🔎 巡邏頻道: {channel_url}")
    ydl_opts = {'extract_flat': True, 'playlistend': 5, 'quiet': True, 'no_warnings': True}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(channel_url, download=False)
            if 'entries' in info and info['entries']:
                for entry in info['entries']:
                    if not entry: continue
                    v_id = entry.get('id')
                    v_title = entry.get('title')
                    # 排除 UC 開頭的頻道 ID
                    if v_id and not v_id.startswith('UC') and v_title:
                        return v_id, v_title, f"https://www.youtube.com/watch?v={v_id}"
    except Exception as e:
        logger.error(f"❌ 讀取頻道失敗: {e}")
    return None, None, None

def download_audio_if_not_exists(url, video_id):
    """
    智慧下載：
    1. 檢查檔案是否存在 (用 video_id 當檔名)
    2. 若存在 -> 直接回傳路徑 (不下載)
    3. 若不存在 -> 下載
    """
    # 建立一個專屬的檔名，例如: C:/.../temp_QVlUUZMmJcQ.m4a
    expected_filename = f"temp_{video_id}" 
    expected_path_m4a = os.path.join(BASE_DIR, f"{expected_filename}.m4a")
    expected_path_webm = os.path.join(BASE_DIR, f"{expected_filename}.webm")

    # 【關鍵檢查】如果檔案已經在了，就不要下載！
    if os.path.exists(expected_path_m4a):
        logger.info(f"📂 發現暫存檔 (跳過下載): {expected_path_m4a}")
        return expected_path_m4a
    if os.path.exists(expected_path_webm):
        logger.info(f"📂 發現暫存檔 (跳過下載): {expected_path_webm}")
        return expected_path_webm

    # 如果沒有，才開始下載
    logger.info(f"📥 開始下載: {url}")
    
    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio', 
        # 使用 ID 當作檔名，確保下次能找到
        'outtmpl': os.path.join(BASE_DIR, f'{expected_filename}.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        
        # 再次檢查下載後的檔案
        if os.path.exists(expected_path_m4a): return expected_path_m4a
        if os.path.exists(expected_path_webm): return expected_path_webm
        return None
    except Exception as e:
        logger.error(f"❌ 下載失敗: {e}")
        return None

def analyze_audio(audio_path, title):
    logger.info(f"🧠 AI 分析中: {title}")
    mime = "audio/webm" if audio_path.endswith(".webm") else "audio/mp4"
    myfile = genai.upload_file(audio_path, mime_type=mime)
    
    while myfile.state.name == "PROCESSING":
        time.sleep(2)
        myfile = genai.get_file(myfile.name)

    if myfile.state.name == "FAILED":
        raise ValueError("Audio processing failed on Google Server")

    model = genai.GenerativeModel("gemini-flash-latest")
    
    # 手機版極簡 Prompt
    promptVideo = f"""
    你是一位講話精準、不廢話的台股操盤手。
    請分析影片「{title}」，產出給手機用戶看的「極簡快報」。

    【排版嚴格要求】：
    1. 絕對禁止 Markdown (不要用 ** 或 ## 或表格)。
    2. 禁止長篇大論，每個重點限制在 15 字以內。
    3. 善用 Emoji 讓版面清爽。

    請依照以下格式輸出：

    📢 【影片快篩】(影片標題簡稱)

    🌡️ 市場溫度：(請用 4 個字形容，如：外資大買、震盪洗盤)

    ⚡ 關鍵重點：
    • (重點1 - 精簡短語)
    • (重點2 - 精簡短語)
    • (重點3 - 精簡短語)

    🎯 個股點評：
    (若無個股則分析產業，格式：名稱 - 方向 - 理由)
    🔸 [股票/產業名]
       👉 (🔴買進 / 🟢賣出 / 🟡觀望)
       📝 (一句話理由，10字內)

    🛡️ 操盤建議：
    (給散戶的一個指令，例如：拉回找買點、切勿追高)
    """
    
    result = model.generate_content([myfile, promptVideo])
    return result.text

def send_line(msg):
    try:
        api = LineBotApi(LINE_TOKEN)
        api.push_message(LINE_USER_ID, TextSendMessage(text=msg))
        logger.info("✅ LINE 通知發送成功")
    except Exception as e:
        logger.error(f"❌ LINE 發送失敗: {e}")

# ================= 4. 主迴圈 (智慧版) =================
if __name__ == "__main__":
    logger.info("🤖 股票分析機器人已啟動 (Smart Flow)")
    
    # 預設等待時間
    next_wait_time = 60 

    while True:
        try:
            history = load_history()
            api_limit_hit = False # 標記是否撞到 API 牆
            
            for channel in TARGET_CHANNELS:
                vid, title, url = get_latest_video(channel)
                
                if vid:
                    if vid in history:
                        logger.info(f"😴 [跳過] 已分析: {title}")
                    else:
                        logger.info(f"⚡ [新片] 發現新影片: {title}")
                        
                        # 1. 智慧下載 (檔案在就不載)
                        audio = download_audio_if_not_exists(url, vid)
                        
                        if audio:
                            try:
                                # 2. 嘗試 AI 分析
                                report = f"{url}\n\n"
                                analysis = analyze_audio(audio, title)
                                report += analysis
                                send_line(report)
                                
                                # 3. 只有成功才存檔 + 刪檔
                                save_history(vid)
                                logger.info(f"✅ 任務成功: {title}")
                                
                                if os.path.exists(audio):
                                    os.remove(audio)
                                    logger.info("🗑️ 暫存檔已清除")

                            except Exception as e:
                                err_msg = str(e)
                                logger.error(f"❌ 處理失敗: {err_msg}")
                                
                                # 【關鍵修正】如果是 API 限制 (429/403)，啟動長睡眠
                                if "429" in err_msg or "ResourceExhausted" in err_msg or "403" in err_msg:
                                    logger.warning("⚠️ API 額度已滿或受限！將啟動 15 分鐘冷卻模式...")
                                    api_limit_hit = True
                                else:
                                    # 其他錯誤 (如 AI 聽不懂)，可能要考慮跳過或重試
                                    # 這裡我們先不存檔，讓它下次再試 (但因為有緩存檔案，不會重載)
                                    logger.info("⚠️ 發生非 API 錯誤，保留檔案稍後重試。")

                time.sleep(2)
            
            # 根據是否撞牆決定休息多久
            if api_limit_hit:
                logger.info("⏳ 進入 API 冷卻模式: 休息 15 分鐘 (900秒)...")
                time.sleep(900)
            else:
                logger.info("⏳ 待機 60 秒...")
                time.sleep(60)

        except KeyboardInterrupt:
            logger.warning("👋 程式手動停止")
            break
        except Exception as e:
            logger.critical(f"❌ 發生未預期錯誤: {e}")
            time.sleep(60)