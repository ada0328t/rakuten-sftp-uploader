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

# Flask 初期化
app = Flask(__name__)

# 環境変数をロード
load_dotenv()

# Google 認証情報の読み込み
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

# Google Sheets & Drive 設定
SPREADSHEET_ID = "1_t8pThdb0kFyIyRfNtC-VLsGa6HopgGQoEOqKyisjME"
SHEET_ACCOUNTS = "アカウント管理"
SHEET_RESERVATIONS = "アップロード予約"
FOLDER_ID = "1ykCNsVXqi619OzXwLTqVJIm1WbqWcMgn"

# Google API 初期化
client = gspread.authorize(creds)
drive_service = build("drive", "v3", credentials=creds)

# SFTP 設定
SFTP_HOST = "upload.rakuten.ne.jp"
SFTP_PORT = 22
SFTP_UPLOAD_PATH = "/ritem/batch"

# ✅ スプレッドシートのステータス更新
def update_sheet_status(filename, status, error_message=""):
    """スプレッドシートのステータスを更新"""
    try:
        sheet = client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_RESERVATIONS)
        data = sheet.get_all_values()
        headers = data[0]

        if "ファイル名" not in headers or "ステータス" not in headers:
            print("❌ スプレッドシートに必要なカラムがありません")
            return
        
        filename_col = headers.index("ファイル名")
        status_col = headers.index("ステータス")
        error_col = headers.index("エラーメッセージ") if "エラーメッセージ" in headers else len(headers)

        for i, row in enumerate(data[1:], start=2):
            if row[filename_col] == filename:
                sheet.update_cell(i, status_col + 1, status)
                sheet.update_cell(i, error_col + 1, error_message)
                return
    except Exception as e:
        print(f"❌ スプレッドシート更新エラー: {e}")

# ✅ SFTP アカウント情報取得
def get_sftp_credentials(account_name):
    """Google Sheets から SFTP アカウント情報を取得"""
    try:
        sheet = client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_ACCOUNTS)
        data = sheet.get_all_values()
        headers = data[0]

        for row in data[1:]:
            row_data = dict(zip(headers, row))
            if row_data["アカウント名"] == account_name:
                return row_data["FTP用ユーザー名"], row_data["FTP用パスワード"]
        return None, None
    except Exception as e:
        print(f"❌ アカウント情報取得エラー: {e}")
        return None, None

# ✅ Google Drive のファイル ID を取得
def get_google_drive_file_id(filename):
    """Google Drive 内のファイル ID を取得"""
    try:
        results = drive_service.files().list(
            q=f"'{FOLDER_ID}' in parents and name='{filename}' and trashed=false",
            fields="files(id, name)"
        ).execute()
        files = results.get("files", [])
        return files[0]["id"] if files else None
    except Exception as e:
        print(f"❌ Google Drive ファイル検索エラー: {e}")
        return None

# ✅ API ステータス確認（404 解決）
@app.route("/status", methods=["GET"])
def status():
    """API が動作しているか確認"""
    return jsonify({"status": "running"}), 200

# ✅ 予約状況取得（404 解決）
@app.route("/get_reservations", methods=["GET"])
def get_reservations():
    """Google Sheets から予約データを取得"""
    try:
        sheet = client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_RESERVATIONS)
        data = sheet.get_all_values()
        return jsonify(data), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ✅ SFTP へアップロード
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

        # ✅ SFTP 認証情報取得
        username, password = get_sftp_credentials(account)
        if not username or not password:
            update_sheet_status(filename, "エラー", "FTPアカウント情報が見つかりません")
            return jsonify({"status": "error", "message": "FTPアカウント情報が見つかりません"}), 400

        # ✅ Google Drive からファイル ID を取得
        file_id = get_google_drive_file_id(filename)
        if not file_id:
            update_sheet_status(filename, "エラー", "Google Drive にファイルが見つかりません")
            return jsonify({"status": "error", "message": "Google Drive にファイルが見つかりません"}), 404

        # ✅ 一時ディレクトリ作成
        tmp_dir = "/tmp" if platform.system() != "Windows" else "./tmp"
        os.makedirs(tmp_dir, exist_ok=True)
        file_path = os.path.join(tmp_dir, filename)

        # ✅ Google Drive からファイルをダウンロード
        request_drive = drive_service.files().get_media(fileId=file_id)
        with open(file_path, "wb") as f:
            downloader = MediaIoBaseDownload(f, request_drive)
            done = False
            while not done:
                status, done = downloader.next_chunk()

        print(f"📂 ダウンロード完了: {file_path}")

        # ✅ SFTP 接続
        try:
            transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
            transport.connect(username=username, password=password)
            sftp = paramiko.SFTPClient.from_transport(transport)

            # ✅ SFTP へファイルアップロード
            remote_path = f"{SFTP_UPLOAD_PATH}/{filename}"
            sftp.put(file_path, remote_path)
            print(f"✅ SFTP アップロード成功: {filename}")

            # ✅ アップロード確認（リモートディレクトリ内のファイル一覧取得）
            uploaded_files = sftp.listdir(SFTP_UPLOAD_PATH)
            if filename in uploaded_files:
                print(f"✅ SFTP 内に {filename} が確認されました")
            else:
                print(f"⚠️ SFTP に {filename} が見つかりません")
                update_sheet_status(filename, "エラー", "SFTP アップロード確認失敗")
                return jsonify({"status": "error", "message": "SFTP アップロード確認失敗"}), 500

            # ✅ SFTP 接続を閉じる
            sftp.close()
            transport.close()

        except Exception as sftp_error:
            print(f"❌ SFTP エラー: {sftp_error}")
            update_sheet_status(filename, "エラー", str(sftp_error))
            return jsonify({"status": "error", "message": str(sftp_error)}), 500

        # ✅ Google Drive からファイル削除
        try:
            drive_service.files().delete(fileId=file_id).execute()
            print(f"🗑 Google Drive から {filename} を削除しました")
        except Exception as drive_error:
            print(f"⚠️ Google Drive のファイル削除エラー: {drive_error}")
            update_sheet_status(filename, "注意", f"Google Drive 削除失敗: {drive_error}")

        # ✅ スプレッドシートのステータスを更新
        update_sheet_status(filename, "アップロード完了")
        return jsonify({"status": "success", "message": f"{filename} のアップロード成功"}), 200

    except Exception as e:
        print(f"❌ `/upload_sftp` でエラー: {str(e)}")
        update_sheet_status(filename, "エラー", str(e))
        return jsonify({"status": "error", "message": str(e)}), 500

# ✅ ルートページ（404 解決）
@app.route("/")
def home():
    """アプリのルートページ"""
    return "Flask API is running!", 200

if __name__ == "__main__":
    print("🚀 Flask サーバー起動: ポート 10000")
    app.run(host="0.0.0.0", port=10000, debug=True)

