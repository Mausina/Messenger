"""
Cloud Messenger - FastAPI backend
AWS services used:
- DynamoDB: messages and users storage
- S3: avatars and file uploads
- CloudWatch: logging via boto3 logs handler
- EC2: deployment target

Runs locally with USE_AWS=false using in-memory storage for quick demo.
"""
import os
import json
import uuid
import time
import logging
from decimal import Decimal
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi import Request
from passlib.hash import bcrypt
import jwt

USE_AWS = os.getenv("USE_AWS", "false").lower() == "true"
JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-me")
JWT_ALG = "HS256"
AWS_REGION = os.getenv("AWS_REGION", "eu-west-1")
S3_BUCKET = os.getenv("S3_BUCKET", "messenger-uploads")
DDB_USERS_TABLE = os.getenv("DDB_USERS_TABLE", "messenger_users")
DDB_MESSAGES_TABLE = os.getenv("DDB_MESSAGES_TABLE", "messenger_messages")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("messenger")

# Send logs to CloudWatch when running on AWS
if USE_AWS:
    try:
        import watchtower
        import boto3
        boto3_logs_client = boto3.client("logs", region_name=AWS_REGION)
        cw_handler = watchtower.CloudWatchLogHandler(
            log_group_name="/messenger/app",
            stream_name="ec2-server",
            boto3_client=boto3_logs_client,
        )
        log.addHandler(cw_handler)
        log.info("CloudWatch logging enabled")
    except Exception as e:
        log.warning(f"CloudWatch logging not available: {e}")

# ---------- Storage layer (AWS or in-memory) ----------

def _decimal_to_native(obj):
    """Recursively convert DynamoDB Decimal to int/float for JSON serialization."""
    if isinstance(obj, list):
        return [_decimal_to_native(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _decimal_to_native(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    return obj


class Storage:
    def create_user(self, username: str, password_hash: str) -> dict: ...
    def get_user(self, username: str) -> Optional[dict]: ...
    def save_message(self, room: str, sender: str, content: str, msg_type: str = "text") -> dict: ...
    def get_messages(self, room: str, limit: int = 50) -> List[dict]: ...
    def list_rooms(self, username: str) -> List[str]: ...


class MemoryStorage(Storage):
    def __init__(self):
        self.users: Dict[str, dict] = {}
        self.messages: Dict[str, List[dict]] = {}
        self.reactions: Dict[str, Dict[str, set]] = {}  # msg_id -> emoji -> {usernames}

    def create_user(self, username, password_hash):
        if username in self.users:
            raise ValueError("user exists")
        u = {"username": username, "password_hash": password_hash, "created_at": int(time.time())}
        self.users[username] = u
        return u

    def get_user(self, username):
        return self.users.get(username)

    def save_message(self, room, sender, content, msg_type="text"):
        msg = {
            "id": str(uuid.uuid4()),
            "room": room,
            "sender": sender,
            "content": content,
            "type": msg_type,
            "ts": int(time.time() * 1000),
            "reactions": {},
        }
        self.messages.setdefault(room, []).append(msg)
        return msg

    def get_messages(self, room, limit=50):
        msgs = self.messages.get(room, [])[-limit:]
        # attach current reactions snapshot
        out = []
        for m in msgs:
            m2 = dict(m)
            r = self.reactions.get(m["id"], {})
            m2["reactions"] = {emoji: sorted(users) for emoji, users in r.items() if users}
            out.append(m2)
        return out

    def toggle_reaction(self, msg_id: str, emoji: str, username: str) -> Dict[str, list]:
        bucket = self.reactions.setdefault(msg_id, {}).setdefault(emoji, set())
        if username in bucket:
            bucket.remove(username)
        else:
            bucket.add(username)
        all_emojis = self.reactions.get(msg_id, {})
        return {e: sorted(users) for e, users in all_emojis.items() if users}

    def list_rooms(self, username):
        rooms = set()
        for room, msgs in self.messages.items():
            if any(m["sender"] == username for m in msgs) or room.startswith(f"dm:{username}:") or room.endswith(f":{username}"):
                rooms.add(room)
        rooms.add("general")
        return sorted(rooms)


class AWSStorage(Storage):
    def __init__(self):
        import boto3
        self.ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
        self.users_table = self.ddb.Table(DDB_USERS_TABLE)
        self.messages_table = self.ddb.Table(DDB_MESSAGES_TABLE)
        self.s3 = boto3.client("s3", region_name=AWS_REGION)
        # in-memory reactions cache (acceptable for demo; reactions don't persist across restarts)
        self._reactions: Dict[str, Dict[str, set]] = {}
        log.info("AWS storage initialized")

    def create_user(self, username, password_hash):
        item = {"username": username, "password_hash": password_hash, "created_at": int(time.time())}
        self.users_table.put_item(Item=item, ConditionExpression="attribute_not_exists(username)")
        return item

    def get_user(self, username):
        r = self.users_table.get_item(Key={"username": username})
        return r.get("Item")

    def save_message(self, room, sender, content, msg_type="text"):
        msg = {
            "room": room,
            "ts": int(time.time() * 1000),
            "id": str(uuid.uuid4()),
            "sender": sender,
            "content": content,
            "type": msg_type,
        }
        self.messages_table.put_item(Item=msg)
        return msg

    def get_messages(self, room, limit=50):
        r = self.messages_table.query(
            KeyConditionExpression="room = :r",
            ExpressionAttributeValues={":r": room},
            ScanIndexForward=True,
            Limit=limit,
        )
        items = r.get("Items", [])
        # attach reactions from in-memory cache
        for m in items:
            mid = m.get("id")
            r = self._reactions.get(mid, {})
            m["reactions"] = {emoji: sorted(users) for emoji, users in r.items() if users}
        return items

    def toggle_reaction(self, msg_id: str, emoji: str, username: str) -> Dict[str, list]:
        bucket = self._reactions.setdefault(msg_id, {}).setdefault(emoji, set())
        if username in bucket:
            bucket.remove(username)
        else:
            bucket.add(username)
        all_emojis = self._reactions.get(msg_id, {})
        return {e: sorted(users) for e, users in all_emojis.items() if users}

    def list_rooms(self, username):
        # simplified — in real prod we'd have a separate user-rooms table
        return ["general", f"dm:{username}:friend"]


storage: Storage = AWSStorage() if USE_AWS else MemoryStorage()
log.info(f"Using {'AWS' if USE_AWS else 'in-memory'} storage")

# ---------- Auth ----------

def make_token(username: str) -> str:
    payload = {"sub": username, "exp": datetime.utcnow() + timedelta(days=7)}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def verify_token(token: str) -> Optional[str]:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
        return payload.get("sub")
    except jwt.PyJWTError:
        return None


# ---------- WebSocket connection manager ----------

class ConnectionManager:
    def __init__(self):
        self.active: Dict[str, List[WebSocket]] = {}  # room -> sockets
        self.user_sockets: Dict[WebSocket, tuple] = {}  # socket -> (username, room)

    async def connect(self, ws: WebSocket, room: str, username: str):
        await ws.accept()
        self.active.setdefault(room, []).append(ws)
        self.user_sockets[ws] = (username, room)
        log.info(f"connect: {username} -> {room} (room size {len(self.active[room])})")

    def disconnect(self, ws: WebSocket, room: str):
        if room in self.active and ws in self.active[room]:
            self.active[room].remove(ws)
        info = self.user_sockets.pop(ws, ("?", room))
        log.info(f"disconnect: {info[0]} from {room}")

    def online_users(self, room: str) -> List[str]:
        users = set()
        for ws in self.active.get(room, []):
            info = self.user_sockets.get(ws)
            if info:
                users.add(info[0])
        return sorted(users)

    async def broadcast(self, room: str, message: dict, exclude: Optional[WebSocket] = None):
        dead = []
        for ws in self.active.get(room, []):
            if ws is exclude:
                continue
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active[room].remove(ws)


manager = ConnectionManager()

# ---------- App ----------

app = FastAPI(title="Cloud Messenger")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/health")
async def health():
    return {"status": "ok", "storage": "aws" if USE_AWS else "memory"}


@app.post("/api/register")
async def register(payload: dict):
    username = (payload.get("username") or "").strip().lower()
    password = payload.get("password") or ""
    if not username or len(password) < 4:
        raise HTTPException(400, "username and password (min 4 chars) required")
    if storage.get_user(username):
        raise HTTPException(409, "user already exists")
    pw_hash = bcrypt.hash(password)
    try:
        storage.create_user(username, pw_hash)
    except Exception as e:
        raise HTTPException(409, str(e))
    log.info(f"registered: {username}")
    return {"token": make_token(username), "username": username}


@app.post("/api/login")
async def login(payload: dict):
    username = (payload.get("username") or "").strip().lower()
    password = payload.get("password") or ""
    user = storage.get_user(username)
    if not user or not bcrypt.verify(password, user["password_hash"]):
        raise HTTPException(401, "invalid credentials")
    log.info(f"login: {username}")
    return {"token": make_token(username), "username": username}


@app.get("/api/messages/{room}")
async def get_messages(room: str, token: str):
    user = verify_token(token)
    if not user:
        raise HTTPException(401, "invalid token")
    return {"messages": storage.get_messages(room)}


@app.post("/api/upload")
async def upload(token: str = Form(...), file: UploadFile = File(...)):
    user = verify_token(token)
    if not user:
        raise HTTPException(401, "invalid token")
    contents = await file.read()
    if len(contents) > 5 * 1024 * 1024:
        raise HTTPException(413, "file too large (max 5MB)")
    key = f"uploads/{user}/{uuid.uuid4()}-{file.filename}"

    if USE_AWS:
        import boto3
        s3 = boto3.client("s3", region_name=AWS_REGION)
        s3.put_object(Bucket=S3_BUCKET, Key=key, Body=contents, ContentType=file.content_type)
        url = f"https://{S3_BUCKET}.s3.{AWS_REGION}.amazonaws.com/{key}"
    else:
        os.makedirs("static/uploads", exist_ok=True)
        local_path = f"static/uploads/{uuid.uuid4()}-{file.filename}"
        with open(local_path, "wb") as f:
            f.write(contents)
        url = f"/{local_path}"

    log.info(f"upload by {user}: {key} ({len(contents)} bytes)")
    return {"url": url, "filename": file.filename}


@app.websocket("/ws/{room}")
async def websocket_endpoint(websocket: WebSocket, room: str, token: str):
    username = verify_token(token)
    if not username:
        await websocket.close(code=1008)
        return

    await manager.connect(websocket, room, username)

    # send chat history on join
    history = storage.get_messages(room)
    history_payload = []
    for m in history:
        m2 = dict(m)
        m2["msg_type"] = m2.pop("type", "text")
        history_payload.append(m2)
    history_payload = _decimal_to_native(history_payload)
    await websocket.send_json({"type": "history", "messages": history_payload})

    # send presence to everyone
    online = manager.online_users(room)
    await manager.broadcast(room, {"type": "presence", "users": online})
    await manager.broadcast(room, {
        "type": "system",
        "content": f"{username} joined",
        "ts": int(time.time() * 1000),
    })

    try:
        while True:
            data = await websocket.receive_json()
            kind = data.get("kind", "message")

            if kind == "typing":
                # broadcast to others (not sender)
                await manager.broadcast(room, {
                    "type": "typing",
                    "username": username,
                    "is_typing": bool(data.get("is_typing")),
                }, exclude=websocket)
                continue

            if kind == "reaction":
                msg_id = data.get("msg_id")
                emoji = data.get("emoji")
                if not msg_id or not emoji:
                    continue
                reactions = storage.toggle_reaction(msg_id, emoji, username)
                await manager.broadcast(room, {
                    "type": "reaction_update",
                    "msg_id": msg_id,
                    "reactions": reactions,
                })
                continue

            # default: chat message
            content = (data.get("content") or "").strip()
            msg_type = data.get("msg_type", "text")
            if not content:
                continue
            saved = storage.save_message(room, username, content, msg_type)
            payload = {"type": "message", "msg_type": saved.pop("type"), **saved}
            payload = _decimal_to_native(payload)
            await manager.broadcast(room, payload)
    except WebSocketDisconnect:
        manager.disconnect(websocket, room)
        online = manager.online_users(room)
        await manager.broadcast(room, {"type": "presence", "users": online})
        await manager.broadcast(room, {
            "type": "system",
            "content": f"{username} left",
            "ts": int(time.time() * 1000),
        })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))