import json
import os
import re
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
import anthropic

app = Flask(__name__, static_folder="public")

DATABASE_URL = os.environ.get("DATABASE_URL")  # Render PostgreSQL

SYSTEM_PROMPT = """あなたは金融経済の学習コーチです。ユーザーが今日学んだことを日本語で入力します。
それを分析して、以下のJSON形式で必ず返答してください。JSONのみ返し、説明文や```は不要です。

{
  "concepts": [
    {"name": "概念名", "highlight": true/false, "desc": "その概念の一言説明（40字以内）"}
  ],
  "description": "今日の学習に対する解説（200字程度）",
  "flowDiagram": "Mermaid flowchart TDのコード（日本語ラベルOK、ノード数6〜12）",
  "networkNodes": [
    {"id": 1, "label": "ノード名", "group": "グループ名", "size": 数値}
  ],
  "networkEdges": [
    {"from": 1, "to": 2, "label": "関係"}
  ],
  "suggestions": [
    {"title": "次に学ぶべきトピック名", "desc": "なぜ学ぶとよいかの説明（60字程度）"}
  ],
  "progress": [
    {"label": "分野名", "pct": 0〜100の数値}
  ]
}

ルール:
- concepts は 4〜6 個。最も中心的な概念1つに "highlight": true をつける。desc は必ず全概念に入れること
- flowDiagram は因果の流れを示すフローチャート。ノードIDに使えるのは英数字のみ
- networkNodes のグループは policy/market/corp/valuation/bond/stock/macro/fx のいずれか
- networkNodes の size は重要度に応じて 14〜30 の整数
- networkEdges は 8〜15 本程度
- suggestions は 3 件
- progress はユーザーの学習内容から推測して 4〜5 分野。値は累積学習量のイメージで設定"""


# ── DB 抽象化（SQLite / PostgreSQL 両対応）──────────────────

def get_db():
    if DATABASE_URL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DATABASE_URL)
        return conn, "pg"
    else:
        import sqlite3
        conn = sqlite3.connect(os.path.join(os.path.dirname(__file__), "journal.db"))
        conn.row_factory = sqlite3.Row
        return conn, "sqlite"


def init_db():
    conn, kind = get_db()
    try:
        cur = conn.cursor()
        if kind == "pg":
            cur.execute("""
                CREATE TABLE IF NOT EXISTS entries (
                    id SERIAL PRIMARY KEY,
                    date TEXT NOT NULL,
                    text TEXT NOT NULL,
                    result TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
        else:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    text TEXT NOT NULL,
                    result TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
        conn.commit()
    finally:
        conn.close()


def db_fetchall(query, params=()):
    conn, kind = get_db()
    try:
        cur = conn.cursor()
        if kind == "pg":
            cur.execute(query.replace("?", "%s"), params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        else:
            cur.execute(query, params)
            return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def db_fetchone(query, params=()):
    rows = db_fetchall(query, params)
    return rows[0] if rows else None


def db_execute(query, params=()):
    conn, kind = get_db()
    try:
        cur = conn.cursor()
        cur.execute(query.replace("?", "%s") if kind == "pg" else query, params)
        conn.commit()
    finally:
        conn.close()


# ── ルート ─────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("public", "index.html")


@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory("public", filename)


@app.route("/api/analyze", methods=["POST"])
def analyze():
    data = request.get_json()
    text = (data.get("text") or "").strip()
    api_key = (data.get("apiKey") or "").strip()
    model = data.get("model") or "claude-sonnet-4-6"

    if not text:
        return jsonify({"error": "テキストを入力してください"}), 400
    if not api_key:
        return jsonify({"error": "APIキーを設定してください"}), 400

    raw = ""
    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=2048,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": text}],
        )
        raw = response.content[0].text.strip()
        match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', raw)
        if match:
            raw = match.group(1).strip()
        result = json.loads(raw)
    except anthropic.AuthenticationError:
        return jsonify({"error": "APIキーが無効です。設定を確認してください。"}), 401
    except json.JSONDecodeError as e:
        print(f"[JSONDecodeError] {e}\nRaw:\n{raw}")
        return jsonify({"error": "AIの応答を解析できませんでした。もう一度お試しください。"}), 500
    except Exception as e:
        print(f"[Error] {type(e).__name__}: {e}")
        return jsonify({"error": str(e)}), 500

    now = datetime.now()
    today = f"{now.year}年{now.month}月{now.day}日"
    db_execute(
        "INSERT INTO entries (date, text, result, created_at) VALUES (?, ?, ?, ?)",
        (today, text, json.dumps(result, ensure_ascii=False), now.isoformat()),
    )
    return jsonify({"result": result, "date": today})


@app.route("/api/entries", methods=["GET"])
def get_entries():
    rows = db_fetchall(
        "SELECT id, date, text, result FROM entries ORDER BY created_at DESC LIMIT 30"
    )
    entries = []
    for row in rows:
        r = json.loads(row["result"])
        tag = r.get("concepts", [{}])[0].get("name", "")
        entries.append({"id": row["id"], "date": row["date"], "text": row["text"], "tag": tag})
    return jsonify(entries)


@app.route("/api/entries/<int:entry_id>", methods=["GET"])
def get_entry(entry_id):
    row = db_fetchone(
        "SELECT date, text, result FROM entries WHERE id = ?", (entry_id,)
    )
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"date": row["date"], "text": row["text"], "result": json.loads(row["result"])})


if __name__ == "__main__":
    init_db()
    app.run(port=3333, debug=False)
