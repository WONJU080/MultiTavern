const assert = require("node:assert/strict");
const { test } = require("node:test");
const { readFileSync } = require("node:fs");
const { randomUUID, createHash } = require("node:crypto");
const vm = require("node:vm");
const path = require("node:path");

const source = readFileSync(path.join(__dirname, "../static/js/app.js"), "utf8");

function storage() {
    const values = new Map();
    return {
        getItem: (key) => values.get(key) ?? null,
        setItem: (key, value) => values.set(key, value),
    };
}

// Run the real client script with isolated browser surfaces; no server or inference.
function browser(localStorage = storage(), sessionStorage = storage()) {
    const nodes = new Map();
    function node(id) {
        if (!nodes.has(id)) nodes.set(id, {
            hidden: id === "grid-container", disabled: false, value: "", textContent: "",
            dataset: {}, style: {}, children: [], listeners: {},
            classList: { add() {}, remove() {}, toggle() {} },
            addEventListener(type, callback) { this.listeners[type] = callback; },
            setAttribute() {}, remove() {},
            querySelector() { return node("button"); },
            querySelectorAll() { return []; },
            append() {}, appendChild() {}, replaceChildren() {}, focus() {},
        });
        return nodes.get(id);
    }
    const sockets = [];
    class Socket {
        static CONNECTING = 0;
        static OPEN = 1;
        static CLOSING = 2;
        constructor(url) {
            this.url = url;
            this.readyState = 0;
            this.listeners = {};
            this.sent = [];
            sockets.push(this);
        }
        addEventListener(type, callback) { this.listeners[type] = callback; }
        open() { this.readyState = 1; this.listeners.open(); }
        send(data) { this.sent.push(JSON.parse(data)); }
        close() { this.readyState = 3; this.listeners.close?.(); }
        receive(type, payload) { this.listeners.message({ data: JSON.stringify({ type, payload }) }); }
    }
    const timers = new Map();
    let timerId = 0;
    const setTimeout = (callback, delay) => {
        timers.set(++timerId, { callback, delay });
        return timerId;
    };
    const clearTimeout = (id) => timers.delete(id);
    const window = {
        location: { protocol: "https:", host: "game.test:4141" },
        crypto: { randomUUID }, localStorage, sessionStorage, setTimeout,
        navigator: { clipboard: { writeText: async () => {} } },
        confirm: () => true,
    };
    const runtime = {
        window, sessionStorage, WebSocket: Socket, setTimeout, clearTimeout, console,
        document: { getElementById: node, createElement: node },
    };
    vm.runInNewContext(source, runtime);
    return {
        runtime,
        sockets, node, localStorage, sessionStorage, hash: runtime.fallbackSha256,
        async login(name = "Arxs", inviteCode = "ABC-123") {
            node("name-input").value = name;
            node("invite-input").value = inviteCode;
            await node("login-form").listeners.submit({ preventDefault() {} });
        },
        async pick(character = "金元珠") {
            sockets[0].receive("room_info", {
                invite_code: "ABC-123",
                state: "AWAITING_PLAYERS",
                accepting_new: true,
                characters: [{ name: character, description: "", claimed_by: null, connected: false }],
            });
            node("button").listeners.click();
        },
        accept(socket = sockets.at(-1), name = "Arxs", token = "private-token") {
            socket.receive("auth_ok", {
                name, character: "金元珠", reconnect_token: token, is_host: false,
                state: "ACTIVE_TURN", invite_code: "ABC-123",
                players: [{ name, character: "金元珠", connected: true, is_host: false }],
                player_order: ["金元珠"],
                characters: [{ name: "金元珠", description: "", claimed_by: name, connected: true }],
            });
        },
        reconnect() {
            const timer = [...timers.values()].find(({ delay }) => delay === 1000);
            assert.ok(timer, "A reconnect should be scheduled");
            timer.callback();
            return sockets.at(-1);
        },
    };
}

async function joined(local, session) {
    const tab = browser(local, session);
    tab.sockets[0].open();
    await tab.login();
    await tab.pick();
    tab.accept();
    return tab;
}

test("same-tab disconnect automatically rejoins with the saved character", async () => {
    const tab = await joined();
    const first = tab.sockets[0];
    first.close();
    const next = tab.reconnect();
    assert.equal(next.url, first.url);
    next.open();
    assert.equal(next.sent[0].event_type, "join_room");
    assert.equal(next.sent[0].data.reconnect_token, "private-token");
    assert.equal(next.sent[0].data.invite_code, "ABC-123");
    assert.equal(next.sent[0].data.character, "金元珠");
    assert.equal(tab.node("chat-input").disabled, true);
    tab.accept(next);
    assert.equal(tab.node("login-modal").hidden, true);
    assert.equal(tab.node("chat-input").disabled, false);
});

test("a reopened tab recovers its identity after name, invite code and character pick", async () => {
    const first = await joined();
    const next = browser(first.localStorage);
    const pending = next.sockets[0];
    pending.open();
    assert.equal(pending.sent.length, 0, "Persistent storage must not auto-login");
    await next.login("arxs");
    assert.equal(pending.sent[0].event_type, "room_info");
    await next.pick();
    const recovered = next.sockets.at(-1);
    assert.equal(recovered.url, first.sockets[0].url);
    assert.notEqual(recovered, pending);
    recovered.open();
    assert.equal(recovered.sent[0].data.reconnect_token, "private-token");
    assert.equal(recovered.sent[0].data.character, "金元珠");
    assert.equal(recovered.sent[0].data.invite_code, "ABC-123");
    next.accept(recovered);
    pending.close();
    pending.receive("error", { msg: "Old socket error" });
    assert.equal(next.node("connection-status").textContent, "Connected");
    assert.equal(next.node("login-modal").hidden, true);
    const record = JSON.parse(first.localStorage.getItem("artificialDungeonIdentity:arxs"));
    assert.deepEqual(Object.keys(record).sort(), ["clientId", "reconnectToken"]);
});

test("reloading the original tab keeps automatic room rejoin", async () => {
    const first = await joined();
    const reloaded = browser(first.localStorage, first.sessionStorage);
    reloaded.sockets[0].open();
    assert.equal(reloaded.sockets[0].url, first.sockets[0].url);
    assert.equal(reloaded.sockets[0].sent[0].event_type, "join_room");
    assert.equal(reloaded.sockets[0].sent[0].data.reconnect_token, "private-token");
    assert.equal(reloaded.sockets[0].sent[0].data.character, "金元珠");
});

test("different players in new tabs keep separate identities and characters", async () => {
    const first = await joined();
    const other = browser(first.localStorage);
    other.sockets[0].open();
    await other.login("Other", "DEF-456");
    assert.equal(other.sockets[0].sent[0].event_type, "room_info");
    await other.pick("Ram");
    assert.equal(other.sockets[0].sent[1].event_type, "join_room");
    assert.equal(other.sockets[0].sent[1].data.character, "Ram");
    assert.equal(other.sockets[0].sent[1].data.reconnect_token, undefined);
    assert.notEqual(other.sockets[0].url, first.sockets[0].url);
    other.accept(other.sockets[0], "Other", "other-token");
    assert.notEqual(first.localStorage.getItem("artificialDungeonIdentity:arxs"),
        first.localStorage.getItem("artificialDungeonIdentity:other"));
});

test("claimed characters are disabled in the picker", async () => {
    const tab = browser();
    tab.sockets[0].open();
    await tab.login();
    tab.sockets[0].receive("room_info", {
        invite_code: "ABC-123",
        state: "ACTIVE_TURN",
        accepting_new: true,
        characters: [
            { name: "金元珠", description: "", claimed_by: "Arxs", connected: true },
            { name: "Ram", description: "", claimed_by: null, connected: false },
        ],
    });
    const button = tab.node("button");
    assert.equal(button.textContent, "Ram");
    button.listeners.click();
    assert.equal(tab.sockets[0].sent[1].event_type, "join_room");
    assert.equal(tab.sockets[0].sent[1].data.character, "Ram");
});

test("room creation binds a digested admin password to the client ID", async () => {
    const tab = browser();
    tab.sockets[0].open();
    tab.runtime.setLoginMode("create");
    tab.node("name-input").value = "Host";
    tab.node("password-input").value = "secret-admin";
    await tab.node("login-form").listeners.submit({ preventDefault() {} });
    const socket = tab.sockets[0];
    assert.equal(socket.sent[0].event_type, "create_room");
    const id = socket.url.split("/").at(-1);
    assert.equal(socket.sent[0].data.admin_password_digest,
        createHash("sha256").update("secret-admin" + id).digest("hex"));
    assert.equal(socket.sent[0].data.password, undefined);
    socket.receive("error", { msg: "Invalid admin password." });
    assert.equal(tab.node("login-modal").hidden, false);
    assert.equal(tab.node("chat-input").disabled, true);
});

test("a room-closed notice stops reconnects and returns to the login screen", async () => {
    const tab = await joined();
    tab.sockets[0].receive("room_closed", { msg: "The room was closed." });
    assert.equal(tab.node("login-modal").hidden, false);
    assert.equal(tab.node("login-error").textContent, "The room was closed.");
    tab.sockets[0].close();
    assert.equal(tab.sockets.length, 1, "No reconnect socket should be created");
});

test("blocked storage still permits login and in-memory socket recovery", async () => {
    const blocked = {
        getItem() { throw new Error("Blocked"); },
        setItem() { throw new Error("Blocked"); },
    };
    const tab = await joined(blocked, blocked);
    tab.sockets[0].close();
    const next = tab.reconnect();
    next.open();
    assert.equal(next.sent[0].data.reconnect_token, "private-token");
});

test("corrupt stored JSON does not prevent a fresh login", async () => {
    const local = storage();
    const session = storage();
    local.setItem("artificialDungeonIdentity:arxs", "not JSON");
    session.setItem("artificialDungeonAuth", "not JSON");
    const tab = await joined(local, session);
    assert.equal(tab.node("login-modal").hidden, true);
});

for (const value of ["", "abc", "x".repeat(55), "x".repeat(56), "x".repeat(64),
    "x".repeat(200), "Salasana 🌲 äö漢字"]) {
    test(`fallback SHA-256 matches standard hashing for ${value.length} characters`, () => {
        assert.equal(browser().hash(value), createHash("sha256").update(value).digest("hex"));
    });
}

test("skip vote panel follows the turn and reports progress", async () => {
    const tab = await joined();
    const ownId = tab.sockets[0].url.split("/").at(-1);
    tab.runtime.applyTurn(null, null);
    assert.equal(tab.node("skip-vote-box").hidden, true);
    tab.runtime.applyTurn("someone-else", "Host");
    assert.equal(tab.node("skip-vote-box").hidden, false);
    tab.node("skip-vote-button").listeners.click();
    assert.equal(tab.sockets[0].sent.at(-1).event_type, "skip_vote");
    assert.equal(tab.node("skip-vote-button").disabled, true);
    tab.sockets[0].receive("skip_vote", { target: "Host", voter: "Arxs", votes: 1, needed: 2 });
    assert.equal(tab.node("skip-vote-status").textContent, "Host: 1/2");
    tab.runtime.applyTurn(ownId, "金元珠");
    assert.equal(tab.node("skip-vote-box").hidden, true);
    tab.runtime.applyTurn("someone-else", "Host");
    assert.equal(tab.node("skip-vote-button").disabled, false);
    assert.equal(tab.node("skip-vote-status").textContent, "");
});

test("a removed player returns to the login screen and can rejoin", async () => {
    const tab = await joined();
    tab.sockets[0].receive("removed", { msg: "You were removed by vote." });
    assert.equal(tab.node("login-modal").hidden, false);
    assert.equal(tab.node("login-error").textContent, "You were removed by vote.");
    assert.equal(tab.node("grid-container").hidden, true);
    assert.equal(tab.sockets[0].readyState, 1, "Socket stays open for rejoining");
    await tab.login("Arxs", "ABC-123");
    await tab.pick();
    assert.equal(tab.sockets[0].sent.at(-1).event_type, "join_room");
    assert.equal(tab.sockets[0].sent.at(-1).data.character, "金元珠");
});

test("opening uses generated text and snapshots keep it separate from later rounds", async () => {
    const tab = await joined();
    const shown = [];
    tab.runtime.appendScenario = (text) => {
        if (text) shown.push(["opening", text]);
    };
    tab.runtime.appendState = (text, round) => shown.push(["state", text, round]);
    tab.runtime.startRound = () => {};
    tab.runtime.syncActions = () => {};
    tab.runtime.markRoundComplete = () => {};
    tab.runtime.renderPlayers = () => {};
    tab.node("log-pane").querySelector = () => null;
    const snapshot = {
        state: "AWAITING_PLAYERS", players: [], player_order: [], characters: [],
        scenario_state: "Lobby preparation",
    };
    tab.runtime.applySnapshot(snapshot);
    assert.deepEqual(shown, []);
    shown.length = 0;
    tab.sockets[0].receive("state_update", {
        global_narrative: "Host the ranger and Player the mage arrive.", player_resolutions: {},
    });
    assert.equal(shown.length, 1);
    assert.equal(shown[0][1], "Host the ranger and Player the mage arrive.");
    shown.length = 0;
    tab.runtime.applySnapshot({
        ...snapshot, state: "ACTIVE_TURN", round_number: 1,
        opening_scenario: "Generated opening", scenario_state: "Generated opening",
    });
    assert.deepEqual(shown, [["opening", "Generated opening"]]);
    shown.length = 0;
    tab.runtime.applySnapshot({
        ...snapshot, state: "ACTIVE_TURN", round_number: 3, completed_round_number: 2,
        opening_scenario: "Generated opening", scenario_state: "Later state",
    });
    assert.deepEqual(shown, [
        ["opening", "Generated opening"], ["state", "Later state", 2],
    ]);
});

test("leave room resets to login without closing the running room", async () => {
    const tab = await joined();
    assert.equal(tab.node("leave-room-button").hidden, false);
    tab.runtime.leaveRoom();
    assert.equal(tab.node("login-modal").hidden, false);
    assert.equal(tab.node("leave-room-button").hidden, true);
    assert.equal(tab.node("grid-container").hidden, true);
    assert.ok(/left the room/i.test(tab.node("login-error").textContent));
    // The socket was closed and no reconnect should be scheduled.
    assert.equal(tab.sockets[0].readyState, 3);
});

test("rejoining after leave reconnects and sends room_info", async () => {
    const tab = await joined();
    tab.runtime.leaveRoom();
    assert.equal(tab.sockets[0].readyState, 3);
    await tab.login("Arxs", "ABC-123");
    const next = tab.sockets.at(-1);
    assert.notEqual(next, tab.sockets[0]);
    next.open();
    assert.equal(next.sent[0].event_type, "room_info");
    assert.equal(next.sent[0].data.invite_code, "ABC-123");
});

test("creating a room after leave reconnects and sends create_room", async () => {
    const tab = await joined();
    tab.runtime.leaveRoom();
    tab.runtime.setLoginMode("create");
    tab.node("name-input").value = "Host";
    tab.node("password-input").value = "secret-admin";
    await tab.node("login-form").listeners.submit({ preventDefault() {} });
    const next = tab.sockets.at(-1);
    assert.notEqual(next, tab.sockets[0]);
    next.open();
    assert.equal(next.sent[0].event_type, "create_room");
    assert.equal(next.sent[0].data.admin_password_digest,
        createHash("sha256").update("secret-admin" + next.url.split("/").at(-1)).digest("hex"));
});

test("reconnect tokens are remembered per room", async () => {
    const tab = await joined();
    const clientId = tab.sockets[0].url.split("/").at(-1);
    const record = JSON.stringify({ clientId, reconnectToken: "private-token" });
    assert.equal(tab.localStorage.getItem("artificialDungeonIdentity:arxs:ABC123"), record);
    assert.equal(tab.runtime.rememberedIdentity("arxs", "ABC-123").reconnectToken, "private-token");
    assert.equal(tab.runtime.rememberedIdentity("arxs", "ABC-123").clientId, clientId);
    assert.equal(tab.runtime.rememberedIdentity("arxs", "ZZZ-999").reconnectToken, "private-token");
});
