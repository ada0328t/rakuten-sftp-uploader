from flask import Flask, request, jsonify
import paramiko
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from dotenv import load_dotenv
import os
import json
import base64
import platform
import io

# 📌 Flask アプリの初期化
app = Flask(__name__)

# 📌 環境変数をロード
load_dotenv()

# 📌 Google 認証情報を Base64 からデコード
creds_json_base64 = os.getenv("GOOGLE_CREDENTIALS_JSON")
if not creds_json_base64:
    raise ValueError("❌ 環境変数 GOOGLE_CREDENTIALS_JSON が設定されていません")

try:
    creds_json_str = base64.b64decode(creds_json_base64).decode("utf-8")
    creds_dict = json.loads(creds_json_str)
    creds = Credentials.from_service_account_info(creds_dict, scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ])
except Exception as e:
    raise ValueError(f"❌ GOOGLE_CREDENTIALS_JSON のデコードに失敗しました: {e}")

# 📌 Google Sheets & Google Drive 設定
SPREADSHEET_ID = "1_t8pThdb0kFyIyRfNtC-VLsGa6HopgGQoEOqKyisjME"
SHEET_ACCOUNTS = "アカウント管理"
SHEET_RESERVATIONS = "アップロード予約"
FOLDER_ID = "1ykCNsVXqi619OzXwLTqVJIm1WbqWcMgn"

# 📌 Gspread & Google Drive API の初期化
client = gspread.authorize(creds)
drive_service = build("drive", "v3", credentials=creds)

# 📌 SFTP 設定
SFTP_HOST = "upload.rakuten.ne.jp"
SFTP_PORT = 22
SFTP_UPLOAD_PATH = "/ritem/batch"

# 📌 スプレッドシートのステータス更新
def update_sheet_status(filename, status, error_message=""):
    """スプレッドシートのステータスを更新"""
    try:
        sheet = client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_RESERVATIONS)
        data = sheet.get_all_values()

        if not data:
            print("❌ スプレッドシートが空です")
            return
        
        headers = data[0]
        filename_col = headers.index("ファイル名")
        status_col = headers.index("ステータス")
        error_col = headers.index("エラーメッセージ") if "エラーメッセージ" in headers else len(headers)

        for i, row in enumerate(data[1:], start=2):
            if row[filename_col] == filename:
                sheet.update_cell(i, status_col + 1, status)
                sheet.update_cell(i, error_col + 1, error_message)
                return
    except Exception as e:
        print(f"❌ スプレッドシート更新エラー: {str(e)}")

# 📌 SFTPへアップロード
@app.route("/upload_sftp", methods=["POST"])
def upload_sftp():
    """Google Drive からファイルをダウンロードし SFTP へアップロード"""
    try:
        data = request.get_json()
        print(f"📌 受信データ: {data}")

        account = data.get("account")
        filename = data.get("filename")

        if not account or not filename:
            return jsonify({"status": "error", "message": "アカウントまたはファイル名が不足しています"}), 400

        username, password = get_sftp_credentials(account)
        if not username or not password:
            update_sheet_status(filename, "エラー", "FTPアカウント情報が見つかりません")
            return jsonify({"status": "error", "message": "FTPアカウント情報が見つかりません"}), 400

        file_id = get_google_drive_file_path(filename)
        if not file_id:
            update_sheet_status(filename, "エラー", "Google Drive にファイルが見つかりません")
            return jsonify({"status": "error", "message": "Google Drive にファイルが見つかりません"}), 404

        tmp_dir = "/tmp" if platform.system() != "Windows" else "./tmp"
        os.makedirs(tmp_dir, exist_ok=True)

        file_path = os.path.join(tmp_dir, filename)
        request_drive = drive_service.files().get_media(fileId=file_id)

        with open(file_path, "wb") as f:
            downloader = MediaIoBaseDownload(f, request_drive)
            done = False
            while not done:
                status, done = downloader.next_chunk()

        print(f"📂 ダウンロード完了: {file_path}")

        # 🔍 SFTP アップロードデバッグ
        print(f"🌐 SFTP 接続: {SFTP_HOST}")
        transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
        transport.connect(username=username, password=password)
        sftp = paramiko.SFTPClient.from_transport(transport)

        print(f"📂 SFTP アップロード開始: {filename} → {SFTP_UPLOAD_PATH}/{filename}")
        try:
            sftp.put(file_path, f"{SFTP_UPLOAD_PATH}/{filename}")
            print(f"✅ SFTP アップロード成功: {filename}")
        except Exception as e:
            print(f"❌ SFTP アップロード失敗: {e}")
            update_sheet_status(filename, "エラー", f"SFTPエラー: {e}")
            return jsonify({"status": "error", "message": f"SFTPエラー: {e}"}), 500

        sftp.close()
        transport.close()
        print("✅ SFTP 接続を閉じました")

        drive_service.files().delete(fileId=file_id).execute()
        print(f"🗑 Google Drive から {filename} を削除しました")

        update_sheet_status(filename, "アップロード完了")
        return jsonify({"status": "success", "message": f"{filename} のアップロード成功"}), 200

    except Exception as e:
        print(f"❌ `/upload_sftp` でエラー: {str(e)}")
        update_sheet_status(filename, "エラー", str(e))
        return jsonify({"status": "error", "message": str(e)}), 500

# 📌 ルートページ
@app.route("/")
def home():
    return "Flask API is running!", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000, debug=True)
