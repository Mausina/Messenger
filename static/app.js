let state = {
    token: localStorage.getItem("token"),
    username: localStorage.getItem("username"),
    room: "general",
    ws: null,
    typingTimer: null,
    isTyping: false,
    typingUsers: new Set(),
    typingResetTimers: {},
    msgsById: {},
};

const $ = (id) => document.getElementById(id);

// === AUTH UI ===
let mode = "login";
document.querySelectorAll(".tab").forEach(t => {
    t.onclick = () => {
        document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
        t.classList.add("active");
        mode = t.dataset.tab;
        $("auth-btn").textContent = mode === "login" ? "Login" : "Register";
    };
});

$("auth-btn").onclick = async () => {
    const username = $("username").value.trim().toLowerCase();
    const password = $("password").value;
    $("auth-error").textContent = "";

    try {
        const res = await fetch(`/api/${mode}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ username, password }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "auth failed");

        state.token = data.token;
        state.username = data.username;
        localStorage.setItem("token", data.token);
        localStorage.setItem("username", data.username);
        showChat();
    } catch (e) {
        $("auth-error").textContent = e.message;
    }
};

$("password").addEventListener("keypress", e => {
    if (e.key === "Enter") $("auth-btn").click();
});

$("logout").onclick = () => {
    if (state.ws) state.ws.close();
    localStorage.clear();
    state = { token: null, username: null, room: "general", ws: null,
              typingUsers: new Set(), typingResetTimers: {}, msgsById: {} };
    $("auth").classList.remove("hidden");
    $("chat").classList.add("hidden");
};

// === CHAT ===
function showChat() {
    $("auth").classList.add("hidden");
    $("chat").classList.remove("hidden");
    $("me").textContent = state.username;
    connectRoom("general");
}

function connectRoom(room) {
    if (state.ws) state.ws.close();
    state.room = room;
    state.msgsById = {};
    state.typingUsers.clear();
    Object.values(state.typingResetTimers).forEach(clearTimeout);
    state.typingResetTimers = {};
    updateTypingIndicator();

    $("current-room").textContent = "#" + room;
    $("messages").innerHTML = "";
    $("online-list").innerHTML = "";
    $("status").textContent = "connecting…";
    $("status").classList.add("disconnected");

    document.querySelectorAll("#rooms li").forEach(li => {
        li.classList.toggle("active", li.dataset.room === room);
    });

    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws/${room}?token=${state.token}`);
    state.ws = ws;

    ws.onopen = () => {
        $("status").textContent = "● connected";
        $("status").classList.remove("disconnected");
    };
    ws.onclose = () => {
        $("status").textContent = "● disconnected";
        $("status").classList.add("disconnected");
    };
    ws.onerror = () => {
        $("status").textContent = "● error";
        $("status").classList.add("disconnected");
    };
    ws.onmessage = (ev) => {
        const data = JSON.parse(ev.data);
        if (data.type === "history") {
            data.messages.forEach(addMessage);
        } else if (data.type === "system") {
            addSystemMessage(data.content);
        } else if (data.type === "presence") {
            renderOnline(data.users);
        } else if (data.type === "typing") {
            handleTyping(data.username, data.is_typing);
        } else if (data.type === "reaction_update") {
            updateReactions(data.msg_id, data.reactions);
        } else if (data.type === "message") {
            addMessage(data);
        }
        const m = $("messages");
        m.scrollTop = m.scrollHeight;
    };
}

function renderOnline(users) {
    const ul = $("online-list");
    ul.innerHTML = "";
    users.forEach(u => {
        const li = document.createElement("li");
        li.textContent = u;
        if (u === state.username) li.classList.add("me");
        ul.appendChild(li);
    });
}

function handleTyping(username, isTyping) {
    if (username === state.username) return;
    if (isTyping) {
        state.typingUsers.add(username);
        clearTimeout(state.typingResetTimers[username]);
        state.typingResetTimers[username] = setTimeout(() => {
            state.typingUsers.delete(username);
            updateTypingIndicator();
        }, 4000);
    } else {
        state.typingUsers.delete(username);
        clearTimeout(state.typingResetTimers[username]);
    }
    updateTypingIndicator();
}

function updateTypingIndicator() {
    const el = $("typing-indicator");
    const users = Array.from(state.typingUsers);
    if (users.length === 0) {
        el.textContent = "";
        el.classList.remove("active");
        return;
    }
    let text;
    if (users.length === 1) text = `${users[0]} is typing`;
    else if (users.length === 2) text = `${users[0]} and ${users[1]} are typing`;
    else text = `${users.length} people are typing`;
    el.textContent = text + " ";
    el.classList.add("active");
}

function addMessage(msg) {
    const div = document.createElement("div");
    const isOwn = msg.sender === state.username;
    div.className = "msg " + (isOwn ? "own" : "other");
    div.dataset.msgId = msg.id;

    const time = new Date(msg.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

    let contentHtml;
    const t = msg.msg_type || msg.type || "text";
    if (t === "image") {
        contentHtml = `<img src="${escapeAttr(msg.content)}" alt="image">`;
    } else if (t === "file") {
        const parts = msg.content.split("|");
        const url = parts[0], name = parts[1] || "file";
        contentHtml = `<a class="file-link" href="${escapeAttr(url)}" target="_blank">📎 ${escapeHtml(name)}</a>`;
    } else {
        contentHtml = `<div class="content">${escapeHtml(msg.content)}</div>`;
    }

    div.innerHTML = `
        <button class="react-btn" title="React">😊</button>
        <div class="sender">${escapeHtml(msg.sender)}</div>
        ${contentHtml}
        <div class="reactions"></div>
        <div class="ts">${time}</div>
    `;

    div.querySelector(".react-btn").onclick = (e) => {
        e.stopPropagation();
        showReactionPicker(e.currentTarget, msg.id);
    };

    $("messages").appendChild(div);
    state.msgsById[msg.id] = div;

    if (msg.reactions && Object.keys(msg.reactions).length) {
        updateReactions(msg.id, msg.reactions);
    }
}

function updateReactions(msgId, reactions) {
    const div = state.msgsById[msgId];
    if (!div) return;
    const container = div.querySelector(".reactions");
    container.innerHTML = "";
    Object.entries(reactions).forEach(([emoji, users]) => {
        if (!users.length) return;
        const el = document.createElement("span");
        el.className = "reaction";
        if (users.includes(state.username)) el.classList.add("mine");
        el.title = users.join(", ");
        el.innerHTML = `${emoji} <span class="count">${users.length}</span>`;
        el.onclick = () => sendReaction(msgId, emoji);
        container.appendChild(el);
    });
}

function sendReaction(msgId, emoji) {
    if (!state.ws || state.ws.readyState !== 1) return;
    state.ws.send(JSON.stringify({ kind: "reaction", msg_id: msgId, emoji }));
}

const picker = $("reaction-picker");
let pickerMsgId = null;

function showReactionPicker(anchor, msgId) {
    pickerMsgId = msgId;
    const rect = anchor.getBoundingClientRect();
    picker.classList.remove("hidden");
    picker.style.left = (rect.left - 60) + "px";
    picker.style.top = (rect.top - 44) + "px";
}

picker.querySelectorAll("span").forEach(s => {
    s.onclick = () => {
        if (pickerMsgId) sendReaction(pickerMsgId, s.dataset.emoji);
        picker.classList.add("hidden");
        pickerMsgId = null;
    };
});

document.addEventListener("click", (e) => {
    if (!picker.contains(e.target) && !e.target.classList.contains("react-btn")) {
        picker.classList.add("hidden");
        pickerMsgId = null;
    }
});

function addSystemMessage(text) {
    const div = document.createElement("div");
    div.className = "msg system";
    div.textContent = text;
    $("messages").appendChild(div);
}

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
}
function escapeAttr(s) { return escapeHtml(s); }

$("send-form").onsubmit = (e) => {
    e.preventDefault();
    const input = $("msg-input");
    const text = input.value.trim();
    if (!text || !state.ws || state.ws.readyState !== 1) return;
    state.ws.send(JSON.stringify({ kind: "message", content: text, msg_type: "text" }));
    input.value = "";
    if (state.isTyping) {
        state.isTyping = false;
        state.ws.send(JSON.stringify({ kind: "typing", is_typing: false }));
        clearTimeout(state.typingTimer);
    }
};

$("msg-input").addEventListener("input", () => {
    if (!state.ws || state.ws.readyState !== 1) return;
    const value = $("msg-input").value;
    if (value && !state.isTyping) {
        state.isTyping = true;
        state.ws.send(JSON.stringify({ kind: "typing", is_typing: true }));
    }
    if (!value && state.isTyping) {
        state.isTyping = false;
        state.ws.send(JSON.stringify({ kind: "typing", is_typing: false }));
        clearTimeout(state.typingTimer);
        return;
    }
    clearTimeout(state.typingTimer);
    state.typingTimer = setTimeout(() => {
        if (state.isTyping) {
            state.isTyping = false;
            state.ws.send(JSON.stringify({ kind: "typing", is_typing: false }));
        }
    }, 2500);
});

$("file-input").onchange = async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append("token", state.token);
    fd.append("file", file);
    try {
        const res = await fetch("/api/upload", { method: "POST", body: fd });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "upload failed");
        const isImage = file.type.startsWith("image/");
        const content = isImage ? data.url : `${data.url}|${data.filename}`;
        state.ws.send(JSON.stringify({
            kind: "message",
            content,
            msg_type: isImage ? "image" : "file",
        }));
    } catch (err) {
        alert("Upload failed: " + err.message);
    }
    e.target.value = "";
};

document.querySelectorAll("#rooms li").forEach(li => {
    li.onclick = () => connectRoom(li.dataset.room);
});

$("new-room-btn").onclick = () => {
    const name = $("new-room-input").value.trim().toLowerCase().replace(/[^a-z0-9_-]/g, "");
    if (!name) return;
    if (!document.querySelector(`#rooms li[data-room="${name}"]`)) {
        const li = document.createElement("li");
        li.dataset.room = name;
        li.textContent = "#" + name;
        li.onclick = () => connectRoom(name);
        $("rooms").appendChild(li);
    }
    $("new-room-input").value = "";
    connectRoom(name);
};

if (state.token && state.username) {
    showChat();
}
