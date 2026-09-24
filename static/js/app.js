"use strict";

// Storage can be unavailable in privacy modes. Keep the active tab usable in memory.
function readStored(storageName, key) {
    try {
        return window[storageName].getItem(key);
    } catch {
        return null;
    }
}

function writeStored(storageName, key, value) {
    try {
        window[storageName].setItem(key, value);
    } catch {
        // Automatic socket reconnects can still use this tab's in-memory credentials.
    }
}

function readStoredObject(storageName, key) {
    try {
        const value = JSON.parse(readStored(storageName, key));
        return value && typeof value === "object" && !Array.isArray(value) ? value : null;
    } catch {
        return null;
    }
}

const storedId = readStored("sessionStorage", "artificialDungeonClientId");

// crypto.randomUUID() is unavailable on non-secure HTTP origins except localhost.
// Use a standards-compatible fallback so remote HTTP clients do not fail before
// the WebSocket is even created.
function createClientId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
        return window.crypto.randomUUID();
    }
    if (window.crypto && typeof window.crypto.getRandomValues === "function") {
        const bytes = new Uint8Array(16);
        window.crypto.getRandomValues(bytes);
        bytes[6] = (bytes[6] & 0x0f) | 0x40;
        bytes[8] = (bytes[8] & 0x3f) | 0x80;
        const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0"));
        return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
    }
    const randomHex = () => Math.floor(Math.random() * 16).toString(16);
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (character) => {
        const value = character === "x" ? Number.parseInt(randomHex(), 16) :
            (Number.parseInt(randomHex(), 16) & 0x3) | 0x8;
        return value.toString(16);
    });
}

let clientId = storedId || createClientId();
let savedAuth = readStoredObject("sessionStorage", "artificialDungeonAuth");
writeStored("sessionStorage", "artificialDungeonClientId", clientId);

function identityKey(name) {
    return `artificialDungeonIdentity:${name.trim().toLowerCase()}`;
}

function rememberAuth(auth) {
    savedAuth = auth;
    writeStored("sessionStorage", "artificialDungeonAuth", JSON.stringify(auth));
}

function rememberedIdentity(name) {
    const identity = readStoredObject("localStorage", identityKey(name));
    if (typeof identity?.clientId !== "string" ||
        !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(identity.clientId) ||
        typeof identity.reconnectToken !== "string" || !identity.reconnectToken) {
        return null;
    }
    return identity;
}

const socketScheme = window.location.protocol === "https:" ? "wss" : "ws";
// WAN routes and reverse proxies can drop an initial handshake. Retry the socket
// without requiring the user to reload the login page manually.
let ws;
let connectionTimer;
let reconnectAttempts = 0;
let reconnectTimer;

const elements = {
    grid: document.getElementById("grid-container"),
    loginModal: document.getElementById("login-modal"),
    loginForm: document.getElementById("login-form"),
    loginError: document.getElementById("login-error"),
    name: document.getElementById("name-input"),
    invite: document.getElementById("invite-input"),
    inviteLabel: document.getElementById("invite-label"),
    password: document.getElementById("password-input"),
    passwordLabel: document.getElementById("password-label"),
    modeCreate: document.getElementById("mode-create"),
    modeJoin: document.getElementById("mode-join"),
    loginSubmit: document.getElementById("login-submit"),
    joinStep: document.getElementById("join-step"),
    joinCharacterList: document.getElementById("join-character-list"),
    joinStatus: document.getElementById("join-status"),
    characterEditor: document.getElementById("character-editor"),
    addCharacter: document.getElementById("add-character"),
    hostModal: document.getElementById("host-modal"),
    scenarioStep: document.getElementById("scenario-step"),
    scenarioForm: document.getElementById("scenario-form"),
    scenario: document.getElementById("scenario-input"),
    guidance: document.getElementById("guidance-input"),
    lobbyStep: document.getElementById("lobby-step"),
    startButton: document.getElementById("start-button"),
    hostStatus: document.getElementById("host-status"),
    closeRoomButton: document.getElementById("close-room-button"),
    inviteBox: document.getElementById("invite-box"),
    inviteCode: document.getElementById("invite-code"),
    copyInvite: document.getElementById("copy-invite"),
    title: document.getElementById("scenario-title"),
    identity: document.getElementById("player-identity"),
    log: document.getElementById("log-pane"),
    playerList: document.getElementById("player-list"),
    lobbyPlayerList: document.getElementById("lobby-player-list"),
    lobbyPlayerCount: document.getElementById("lobby-player-count"),
    chatMessages: document.getElementById("chat-messages"),
    chatForm: document.getElementById("chat-form"),
    chatInput: document.getElementById("chat-input"),
    actionForm: document.getElementById("input-pane"),
    actionInput: document.getElementById("action-input"),
    skipVoteBox: document.getElementById("skip-vote-box"),
    skipVoteButton: document.getElementById("skip-vote-button"),
    skipVoteStatus: document.getElementById("skip-vote-status"),
    dmThinking: document.getElementById("dm-thinking"),
    connectionStatus: document.getElementById("connection-status"),
    tokenUsage: document.getElementById("token-usage"),
    tokenChart: document.getElementById("token-chart"),
    tokenCount: document.getElementById("token-count"),
    endGameButton: document.getElementById("end-game-button"),
    retryRoundButton: document.getElementById("retry-round-button"),
};

let authenticated = false;
let isHost = false;
let loginMode = "join";
let roomClosed = false;
let pendingJoin = null;
let myName = "";
let votedFor = false;
let lastStartedRound = 0;
const renderedActions = new Set();
const playerColors = new Map();
const MAX_LOG_ENTRIES = 2000;
const MAX_CHAT_ENTRIES = 1000;

function send(eventType, data) {
    if (ws.readyState !== WebSocket.OPEN) {
        showError("The server connection is not open.");
        return false;
    }
    ws.send(JSON.stringify({ event_type: eventType, data }));
    return true;
}

function trimContainer(container, maximum) {
    while (container.children.length > maximum) {
        const removed = container.firstElementChild?.id === "game-banner"
            ? container.firstElementChild.nextElementSibling : container.firstElementChild;
        if (removed && removed.dataset.actionKey) {
            renderedActions.delete(removed.dataset.actionKey);
        }
        removed?.remove();
    }
}

function appendText(container, text, className, maximum = MAX_LOG_ENTRIES) {
    const entry = document.createElement("p");
    entry.className = className;
    entry.textContent = text;
    container.appendChild(entry);
    trimContainer(container, maximum);
    container.scrollTop = container.scrollHeight;
    return entry;
}

function startRound(roundNumber) {
    if (!Number.isInteger(roundNumber) || roundNumber <= lastStartedRound) {
        return;
    }
    lastStartedRound = roundNumber;
    elements.log.querySelectorAll(".current-round").forEach((entry) => {
        entry.classList.remove("current-round");
    });
    appendText(elements.log, `Round ${roundNumber}:`, "round-heading current-round");
}

function markRoundComplete() {
    elements.log.querySelectorAll(".latest-complete-round").forEach((entry) => {
        entry.classList.remove("latest-complete-round");
    });
    elements.log.querySelectorAll(".current-round").forEach((entry) => {
        entry.classList.add("latest-complete-round");
    });
}

function setPlayerOrder(playerOrder = []) {
    playerOrder.forEach((playerName, index) => {
        if (!playerColors.has(playerName)) {
            playerColors.set(playerName, index % 8);
        }
    });
}

function renderPlayers(payload = {}) {
    const characters = payload.characters || [];
    const players = payload.players || [];
    const connected = players.filter((player) => player.connected).length;
    elements.lobbyPlayerCount.textContent = `${connected} of ${players.length} players connected`;
    [elements.playerList, elements.lobbyPlayerList].forEach((list) => {
        list.replaceChildren();
        characters.forEach((char) => {
            const item = document.createElement("li");
            item.className = char.claimed_by
                ? (char.connected ? "player-online" : "player-offline")
                : "character-unclaimed";
            const dot = document.createElement("span");
            dot.className = "presence-dot";
            dot.setAttribute("aria-hidden", "true");
            const name = document.createElement("span");
            name.textContent = char.claimed_by
                ? `${char.name} · ${char.claimed_by}`
                : `${char.name} · unclaimed`;
            const status = document.createElement("span");
            status.className = "player-presence-label";
            status.textContent = char.claimed_by
                ? (char.connected ? "Connected" : "Disconnected")
                : "Unclaimed";
            item.append(dot, name, status);
            list.appendChild(item);
        });
    });
}

function setThinking(active) {
    elements.dmThinking.hidden = !active;
}

function playerColorClass(playerName) {
    const colorIndex = playerColors.get(playerName);
    return colorIndex === undefined ? "" : ` player-color-${colorIndex}`;
}

function showAction(roundNumber, playerName, action, colorIndex = null) {
    if (Number.isInteger(colorIndex)) {
        playerColors.set(playerName, colorIndex % 8);
    }
    const actionKey = `${roundNumber}:${playerName}:${action}`;
    if (renderedActions.has(actionKey)) {
        return;
    }
    renderedActions.add(actionKey);
    startRound(roundNumber);
    const entry = appendText(
        elements.log,
        `${playerName} attempts: ${action}`,
        `action-entry current-round${playerColorClass(playerName)}`,
    );
    entry.dataset.actionKey = actionKey;
}

function syncActions(roundNumber, submittedActions = {}) {
    Object.entries(submittedActions).forEach(([playerName, action]) => {
        showAction(roundNumber, playerName, action);
    });
}

function displayGameTitle(title) {
    return title ? `Anyworld - ${title}` : "Anyworld";
}

function appendScenario(scenario) {
    if (!scenario || elements.log.querySelector(".opening-scenario")) {
        return;
    }
    const entry = document.createElement("article");
    entry.className = "state-entry opening-entry opening-scenario";
    const label = document.createElement("strong");
    label.className = "state-round-label";
    label.textContent = "Opening scenario";
    const narrative = document.createElement("p");
    narrative.className = "state-narrative";
    narrative.textContent = scenario;
    entry.append(label, narrative);
    const anchor = document.getElementById("game-banner");
    anchor.after(entry);
    trimContainer(elements.log, MAX_LOG_ENTRIES);
}

function appendState(text, roundNumber = null) {
    const entry = document.createElement("article");
    entry.className = "state-entry";
    entry.classList.add(roundNumber === null ? "opening-entry" : "current-round");
    if (roundNumber === null) entry.classList.add("opening-scenario");

    const label = document.createElement("strong");
    label.className = "state-round-label";
    label.textContent = roundNumber ? `Round ${roundNumber} result` : "Opening scenario";

    const narrative = document.createElement("p");
    narrative.className = "state-narrative";
    narrative.textContent = text;

    entry.append(label, narrative);
    elements.log.appendChild(entry);
    trimContainer(elements.log, MAX_LOG_ENTRIES);
    if (roundNumber !== null) {
        elements.log.scrollTop = elements.log.scrollHeight;
    }
}

function showError(message) {
    if (!authenticated) {
        elements.loginModal.hidden = false;
        elements.loginError.textContent = message;
    } else {
        appendText(
            elements.chatMessages,
            `Error: ${message}`,
            "chat-entry error",
            MAX_CHAT_ENTRIES,
        );
    }
}

function showHostStep(state) {
    if (!isHost || ["ACTIVE_TURN", "AWAITING_LLM", "ENDED"].includes(state)) {
        elements.hostModal.hidden = true;
        return;
    }
    elements.hostModal.hidden = false;
    const scenarioReady = state === "AWAITING_PLAYERS";
    elements.scenarioStep.hidden = scenarioReady;
    elements.lobbyStep.hidden = !scenarioReady;
}

function updateSkipVote(activePlayerId) {
    const ownTurn = activePlayerId === clientId;
    elements.skipVoteBox.hidden = ownTurn || !activePlayerId;
    votedFor = false;
    elements.skipVoteButton.disabled = false;
    elements.skipVoteStatus.textContent = "";
}

function applyTurn(activePlayerId, activePlayerName) {
    const ownTurn = activePlayerId === clientId;
    elements.actionInput.disabled = !ownTurn;
    elements.actionInput.placeholder = ownTurn
        ? "Enter your action..."
        : `Waiting for ${activePlayerName || "the next turn"}...`;
    if (ownTurn) {
        elements.actionInput.focus();
    }
    updateSkipVote(activePlayerId);
}

function showInviteCode(code) {
    if (!isHost || !code) {
        return;
    }
    elements.inviteBox.hidden = false;
    elements.inviteCode.textContent = code;
}

function roomWasClosed(message) {
    authenticated = false;
    isHost = false;
    roomClosed = true;
    savedAuth = null;
    writeStored("sessionStorage", "artificialDungeonAuth", null);
    elements.grid.hidden = true;
    elements.chatInput.disabled = true;
    elements.actionInput.disabled = true;
    elements.endGameButton.hidden = true;
    elements.retryRoundButton.hidden = true;
    elements.inviteBox.hidden = true;
    elements.hostModal.hidden = true;
    elements.loginModal.hidden = false;
    elements.loginError.textContent = message;
    elements.connectionStatus.textContent = "Disconnected";
}

function wasRemoved(message) {
    authenticated = false;
    isHost = false;
    savedAuth = null;
    writeStored("sessionStorage", "artificialDungeonAuth", null);
    elements.grid.hidden = true;
    elements.chatInput.disabled = true;
    elements.actionInput.disabled = true;
    elements.endGameButton.hidden = true;
    elements.retryRoundButton.hidden = true;
    elements.inviteBox.hidden = true;
    elements.hostModal.hidden = true;
    elements.skipVoteBox.hidden = true;
    elements.loginModal.hidden = false;
    elements.loginError.textContent = message;
    elements.connectionStatus.textContent = "Connected — rejoin to play";
}

function applySnapshot(payload) {
    isHost = payload.is_host;
    myName = payload.name;
    elements.retryRoundButton.hidden = !isHost || !payload.round_paused;
    elements.actionInput.disabled = true;
    setThinking(payload.state === "AWAITING_LLM" && !payload.round_paused);
    setPlayerOrder(payload.player_order);
    renderPlayers(payload);
    const identity = payload.character ? `${payload.name} (${payload.character})` : payload.name;
    elements.identity.textContent = `${identity}${isHost ? " · Host" : ""}`;
    if (payload.scenario_title) {
        elements.title.textContent = displayGameTitle(payload.scenario_title);
    }
    const emptyLog = !elements.log.querySelector(":scope > :not(#game-banner)");
    appendScenario(payload.opening_scenario);
    if (emptyLog) {
        if (payload.completed_round_number && payload.scenario_state) {
            appendState(payload.scenario_state, payload.completed_round_number);
        }
    }
    if (payload.round_number) {
        startRound(payload.round_number);
        syncActions(payload.round_number, payload.submitted_actions);
    }
    if (payload.state === "ACTIVE_TURN") {
        applyTurn(payload.active_player_id, payload.active_player_name);
    }
    showHostStep(payload.state);
    elements.endGameButton.hidden = !isHost || !["ACTIVE_TURN", "AWAITING_LLM"].includes(payload.state);
}

function handleMessage(message) {
    const { type, payload } = message;
    if (!authenticated && type !== "auth_ok" && type !== "error" && type !== "room_info") {
        return;
    }
    if (type === "auth_ok") {
        rememberAuth({
            ...savedAuth,
            name: payload.name,
            character: payload.character,
            invite_code: payload.invite_code || savedAuth?.invite_code,
            reconnect_token: payload.reconnect_token,
        });
        // Persist only the identity proof, never the password or password digest.
        // A reopened tab still asks for credentials before it can reclaim this player.
        writeStored("localStorage", identityKey(payload.name), JSON.stringify({
            clientId,
            reconnectToken: payload.reconnect_token,
        }));
        authenticated = true;
        roomClosed = false;
        elements.chatInput.disabled = false;
        elements.connectionStatus.textContent = "Connected";
        elements.loginModal.hidden = true;
        elements.grid.hidden = false;
        elements.loginError.textContent = "";
        elements.joinStep.hidden = true;
        applySnapshot(payload);
        showInviteCode(payload.invite_code);
    } else if (type === "room_info") {
        renderCharacterPicker(payload);
    } else if (type === "turn_directive") {
        startRound(payload.round_number);
        syncActions(payload.round_number, payload.submitted_actions);
        applyTurn(payload.active_player_id, payload.active_player_name);
    } else if (type === "round_start") {
        startRound(payload.round_number);
    } else if (type === "action_echo") {
        showAction(
            payload.round_number,
            payload.player_name,
            payload.action,
            payload.player_color_index,
        );
    } else if (type === "state_update") {
        setThinking(false);
        setPlayerOrder(payload.player_order);
        if (payload.round_title) {
            elements.title.textContent = displayGameTitle(payload.round_title);
        }
        startRound(payload.round_number);
        syncActions(payload.round_number, payload.submitted_actions);
        appendState(payload.global_narrative, payload.round_number);
        Object.entries(payload.dice_results || {}).forEach(([player, roll]) => {
            appendText(
                elements.log,
                `🎲 ${player} rolled ${roll}/100`,
                `dice-entry current-round${playerColorClass(player)}`,
            );
        });
        Object.entries(payload.player_resolutions).forEach(([player, resolution]) => {
            appendText(
                elements.log,
                `[${player}] ${resolution}`,
                `resolution-entry current-round${playerColorClass(player)}`,
            );
        });
        markRoundComplete();
    } else if (type === "player_roster") {
        renderPlayers(payload);
    } else if (type === "dm_thinking") {
        setThinking(Boolean(payload.active));
        if (payload.active) {
            elements.retryRoundButton.hidden = true;
            elements.endGameButton.hidden = !isHost;
            elements.hostModal.hidden = true;
        }
    } else if (type === "chat_echo") {
        appendText(
            elements.chatMessages,
            `${payload.name}: ${payload.chat}`,
            "chat-entry",
            MAX_CHAT_ENTRIES,
        );
    } else if (type === "system_msg") {
        appendText(
            elements.chatMessages,
            `System: ${payload.msg}`,
            "chat-entry",
            MAX_CHAT_ENTRIES,
        );
        if (payload.msg === "The game has started.") {
            elements.hostModal.hidden = true;
        }
    } else if (type === "token_usage") {
        elements.tokenUsage.hidden = false;
        elements.tokenUsage.title = payload.counting_method || "Estimated token usage";
        const used = Math.max(0, Number(payload.retained_context_tokens ?? payload.approximate_tokens) || 0);
        const limit = Math.max(1, Number(payload.context_window_size) || used || 1);
        const ratio = Math.min(1, used / limit);
        elements.tokenChart.style.background =
            `conic-gradient(var(--accent) ${ratio * 360}deg, var(--border) ${ratio * 360}deg)`;
        elements.tokenChart.setAttribute(
            "aria-label",
            `Estimated retained context: ${used.toLocaleString()} of ${limit.toLocaleString()}`,
        );
        const formatTotal = (value) => value == null ? "unknown" : value.toLocaleString();
        const round = payload.round || {};
        const game = payload.game || {};
        elements.tokenCount.textContent = `≈ ${used.toLocaleString()} / ${limit.toLocaleString()}`;
        document.getElementById("token-details").textContent = [
            payload.counting_method || "Retained context is estimated; next input, schema and output are excluded.",
            `Context limit source: ${payload.context_window_source || "configured"}`,
            `Round tokens: ${formatTotal(round.total_tokens)} (input ${formatTotal(round.input_tokens)}, output ${formatTotal(round.completion_tokens)})`,
            `Game tokens: ${formatTotal(game.total_tokens)}`,
            `Round cache reads: ${formatTotal(round.cached_tokens)}`,
            `llama.cpp processed / reused: ${formatTotal(round.processed_prompt_tokens)} / ${formatTotal(round.reused_prompt_tokens)}`,
            `Requests: ${round.attempts || 0} · Errors: ${round.errors || 0} · Retries: ${round.retries || 0}`,
            `Failed round attempts: ${payload.round_failures || 0}`,
            `Request time: ${(round.latency_seconds || 0).toFixed(2)}s`,
            `Round work time: ${(payload.round_work_seconds || 0).toFixed(2)}s (includes budgeting and retries)`,
            "Unknown means the provider did not report every counter; tokens are not a currency cost.",
        ].join("\n");
    } else if (type === "game_ended") {
        setThinking(false);
        elements.actionInput.disabled = true;
        elements.endGameButton.hidden = true;
        elements.retryRoundButton.hidden = true;
        appendText(elements.chatMessages, `System: ${payload.msg}`, "chat-entry", MAX_CHAT_ENTRIES);
    } else if (type === "room_closed") {
        roomWasClosed(payload.msg || "This room was closed.");
    } else if (type === "removed") {
        wasRemoved(payload.msg || "You were removed from the game. Rejoin to continue playing.");
    } else if (type === "skip_vote") {
        elements.skipVoteStatus.textContent = `${payload.target}: ${payload.votes}/${payload.needed}`;
        if (payload.voter === myName) {
            votedFor = true;
            elements.skipVoteButton.disabled = true;
        }
    } else if (type === "scenario_ready") {
        elements.hostModal.hidden = false;
        elements.endGameButton.hidden = true;
        elements.title.textContent = payload.title;
        elements.hostStatus.textContent = `“${payload.title}” is ready.`;
        elements.scenarioStep.hidden = true;
        elements.lobbyStep.hidden = false;
        if (payload.characters) {
            renderPlayers({ players: [], characters: payload.characters });
        }
        elements.scenarioForm.querySelector("button").disabled = false;
    } else if (type === "error") {
        showError(payload.msg || "Unknown server error.");
        elements.scenarioForm.querySelector("button").disabled = false;
        elements.startButton.disabled = false;
        if (payload.state) {
            showHostStep(payload.state);
            elements.hostStatus.textContent = payload.msg;
            elements.endGameButton.hidden = !isHost ||
                !["ACTIVE_TURN", "AWAITING_LLM"].includes(payload.state);
        }
        if (payload.round_paused) {
            setThinking(false);
            elements.retryRoundButton.hidden = !isHost;
            elements.actionInput.disabled = true;
        }
    }
}

function connectSocket() {
    clearTimeout(reconnectTimer);
    clearTimeout(connectionTimer);
    const previous = ws;
    const socket = new WebSocket(`${socketScheme}://${window.location.host}/ws/${clientId}`);
    ws = socket;
    authenticated = false;
    elements.actionInput.disabled = true;
    elements.chatInput.disabled = true;
    if (previous && previous.readyState < WebSocket.CLOSING) previous.close();
    connectionTimer = window.setTimeout(() => {
        if (ws === socket && socket.readyState === WebSocket.CONNECTING) socket.close();
    }, 10000);
    socket.addEventListener("open", () => {
        if (ws !== socket) return;
        clearTimeout(connectionTimer);
        reconnectAttempts = 0;
        elements.connectionStatus.textContent = savedAuth ? "Rejoining..." : "Connected";
        if (savedAuth) {
            socket.send(JSON.stringify({ event_type: "join_room", data: savedAuth }));
        }
    });
    socket.addEventListener("message", (event) => {
        if (ws !== socket) return;
        try {
            handleMessage(JSON.parse(event.data));
        } catch (error) {
            console.error("Invalid server message", error);
            showError("Received an invalid server message.");
        }
    });
    socket.addEventListener("close", () => {
        if (ws !== socket) return;
        clearTimeout(connectionTimer);
        authenticated = false;
        elements.connectionStatus.textContent = "Reconnecting...";
        elements.actionInput.disabled = true;
        elements.chatInput.disabled = true;
        if (roomClosed) {
            return;
        }
        const delay = Math.min(1000 * (2 ** reconnectAttempts), 15000);
        reconnectAttempts += 1;
        reconnectTimer = window.setTimeout(connectSocket, delay);
    });
    socket.addEventListener("error", () => {
        if (ws !== socket) return;
        elements.connectionStatus.textContent = "Connection error";
    });
}

connectSocket();

function fallbackSha256(value) {
    const rightRotate = (word, amount) => (word >>> amount) | (word << (32 - amount));
    const maxWord = 2 ** 32;
    const words = [];
    const hash = [];
    const constants = [];
    const composite = {};
    let primeCounter = 0;
    for (let candidate = 2; primeCounter < 64; candidate += 1) {
        if (!composite[candidate]) {
            for (let multiple = candidate * candidate; multiple < 313; multiple += candidate) {
                composite[multiple] = true;
            }
            if (primeCounter < 8) hash[primeCounter] = (candidate ** 0.5 * maxWord) | 0;
            constants[primeCounter] = (candidate ** (1 / 3) * maxWord) | 0;
            primeCounter += 1;
        }
    }
    const encoded = unescape(encodeURIComponent(value));
    for (let index = 0; index < encoded.length; index += 1) {
        words[index >> 2] |= encoded.charCodeAt(index) << (3 - (index % 4)) * 8;
    }
    words[encoded.length >> 2] |= 0x80 << (3 - (encoded.length % 4)) * 8;
    words[((encoded.length + 8) >> 6) * 16 + 15] = encoded.length * 8;
    for (let block = 0; block < words.length; block += 16) {
        const schedule = words.slice(block, block + 16);
        const oldHash = hash.slice();
        for (let index = 0; index < 64; index += 1) {
            const w15 = schedule[index - 15];
            const w2 = schedule[index - 2];
            const a = hash[0];
            const e = hash[4];
            const temp1 = hash[7] + (rightRotate(e, 6) ^ rightRotate(e, 11) ^ rightRotate(e, 25))
                + ((e & hash[5]) ^ (~e & hash[6])) + constants[index]
                + (schedule[index] = index < 16 ? (schedule[index] || 0) :
                    (schedule[index - 16] + (rightRotate(w15, 7) ^ rightRotate(w15, 18) ^ (w15 >>> 3))
                    + schedule[index - 7] + (rightRotate(w2, 17) ^ rightRotate(w2, 19) ^ (w2 >>> 10))) | 0);
            const temp2 = (rightRotate(a, 2) ^ rightRotate(a, 13) ^ rightRotate(a, 22))
                + ((a & hash[1]) ^ (a & hash[2]) ^ (hash[1] & hash[2]));
            hash.pop();
            hash.unshift((temp1 + temp2) | 0);
            hash[4] = (hash[4] + temp1) | 0;
        }
        hash.forEach((valuePart, index) => { hash[index] = (valuePart + oldHash[index]) | 0; });
    }
    return hash.map((word) => (word >>> 0).toString(16).padStart(8, "0")).join("");
}

async function passwordDigest(password, identity = clientId) {
    const value = password + identity;
    // Some mobile browsers expose crypto.subtle but reject it on an insecure HTTP
    // origin. Fall back if the digest operation itself is unavailable or rejected.
    if (window.crypto?.subtle && window.TextEncoder) {
        try {
            const bytes = new TextEncoder().encode(value);
            const digest = await window.crypto.subtle.digest("SHA-256", bytes);
            return Array.from(new Uint8Array(digest), (byte) =>
                byte.toString(16).padStart(2, "0"),
            ).join("");
        } catch (error) {
            console.warn("Web Crypto SHA-256 unavailable; using fallback", error);
        }
    }
    return fallbackSha256(value);
}

elements.loginForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    elements.loginError.textContent = "";
    try {
        const name = elements.name.value.trim();
        if (loginMode === "create") {
            const identity = rememberedIdentity(name);
            const targetId = identity?.clientId || clientId;
            const reconnectToken = identity?.reconnectToken || (
                savedAuth?.name?.toLowerCase() === name.toLowerCase()
                    ? savedAuth.reconnect_token : undefined
            );
            const auth = { name, reconnect_token: reconnectToken };
            const adminPassword = elements.password.value;
            if (adminPassword) {
                auth.admin_password_digest = await passwordDigest(adminPassword, targetId);
            }
            // Persist only the join identity, never an admin password digest.
            rememberAuth({ name, reconnect_token: reconnectToken, invite_code: auth.invite_code });
            roomClosed = false;
            if (targetId !== clientId || ws.readyState !== WebSocket.OPEN) {
                clientId = targetId;
                writeStored("sessionStorage", "artificialDungeonClientId", clientId);
                connectSocket();
            } else {
                send("create_room", auth);
            }
        } else {
            const inviteCode = elements.invite.value.trim();
            pendingJoin = { name, invite_code: inviteCode };
            elements.joinStep.hidden = false;
            elements.joinStatus.textContent = "Loading characters...";
            elements.joinCharacterList.replaceChildren();
            send("room_info", { invite_code: inviteCode });
        }
    } catch (error) {
        showError(error.message);
    }
});

function renderCharacterPicker(payload) {
    const characters = payload.characters || [];
    elements.joinCharacterList.replaceChildren();
    if (!payload.accepting_new) {
        elements.joinStatus.textContent = "This room is not accepting new players.";
        return;
    }
    const available = characters.filter((char) => !char.claimed_by || char.claimed_by === pendingJoin?.name);
    if (!characters.length) {
        elements.joinStatus.textContent = "The room has not defined its cast yet.";
        return;
    }
    if (!available.length) {
        elements.joinStatus.textContent = "Every character has already been claimed.";
        return;
    }
    elements.joinStatus.textContent = "";
    for (const char of characters) {
        const item = document.createElement("button");
        item.type = "button";
        item.className = "character-option";
        const taken = Boolean(char.claimed_by);
        if (taken) {
            item.disabled = true;
        }
        const label = char.description
            ? `${char.name} — ${char.description}`
            : char.name;
        item.textContent = taken
            ? `${label} (claimed by ${char.claimed_by})`
            : label;
        if (!taken) {
            item.addEventListener("click", () => chooseCharacter(char.name));
        }
        elements.joinCharacterList.appendChild(item);
    }
}

function chooseCharacter(character) {
    if (!pendingJoin) {
        return;
    }
    const identity = rememberedIdentity(pendingJoin.name);
    const targetId = identity?.clientId || clientId;
    const reconnectToken = identity?.reconnectToken || (
        savedAuth?.name?.toLowerCase() === pendingJoin.name.toLowerCase()
            ? savedAuth.reconnect_token : undefined
    );
    const auth = {
        name: pendingJoin.name,
        invite_code: pendingJoin.invite_code,
        character,
        reconnect_token: reconnectToken,
    };
    rememberAuth({
        name: auth.name,
        invite_code: auth.invite_code,
        character,
        reconnect_token: auth.reconnect_token,
    });
    roomClosed = false;
    if (targetId !== clientId || ws.readyState !== WebSocket.OPEN) {
        clientId = targetId;
        writeStored("sessionStorage", "artificialDungeonClientId", clientId);
        connectSocket();
    } else {
        send("join_room", auth);
    }
}

function setLoginMode(mode) {
    loginMode = mode;
    const creating = mode === "create";
    elements.modeCreate.classList.toggle("active", creating);
    elements.modeJoin.classList.toggle("active", !creating);
    elements.invite.hidden = creating;
    elements.inviteLabel.hidden = creating;
    elements.password.hidden = !creating;
    elements.passwordLabel.hidden = !creating;
}

elements.modeCreate.addEventListener("click", () => setLoginMode("create"));
elements.modeJoin.addEventListener("click", () => setLoginMode("join"));

elements.copyInvite.addEventListener("click", () => {
    const code = elements.inviteCode.textContent;
    if (!code || !navigator.clipboard?.writeText) {
        return;
    }
    navigator.clipboard.writeText(code).catch(() => {
        // Clipboard can be blocked on insecure origins; the code remains visible.
    });
});

function makeCharacterRow() {
    const row = document.createElement("div");
    row.className = "character-row";
    const mine = document.createElement("input");
    mine.type = "radio";
    mine.name = "host-character";
    mine.className = "character-mine";
    mine.setAttribute("aria-label", "Play this character");
    const name = document.createElement("input");
    name.className = "character-name";
    name.maxLength = 40;
    name.placeholder = "Character name";
    const description = document.createElement("input");
    description.className = "character-description";
    description.maxLength = 50000;
    description.placeholder = "Description (optional)";
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "remove-character";
    remove.textContent = "×";
    remove.addEventListener("click", () => {
        if (elements.characterEditor.children.length > 1) {
            row.remove();
        }
    });
    row.append(mine, name, description, remove);
    elements.characterEditor.appendChild(row);
    return row;
}

function collectCharacters() {
    const rows = [...elements.characterEditor.querySelectorAll(".character-row")];
    const characters = rows.map((row) => ({
        name: row.querySelector(".character-name").value.trim(),
        description: row.querySelector(".character-description").value.trim(),
    }));
    const chosen = elements.characterEditor.querySelector(".character-mine:checked");
    const hostCharacter = chosen
        ? chosen.parentElement.querySelector(".character-name").value.trim()
        : null;
    return { characters, hostCharacter };
}

elements.addCharacter.addEventListener("click", () => {
    makeCharacterRow();
});

elements.scenarioForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const { characters, hostCharacter } = collectCharacters();
    if (characters.some((char) => !char.name)) {
        elements.hostStatus.textContent = "Every character needs a name.";
        return;
    }
    if (!hostCharacter) {
        elements.hostStatus.textContent = "Pick the character you will play.";
        return;
    }
    if (
        send("scenario_init", {
            scenario: elements.scenario.value.trim(),
            guidance: elements.guidance.value.trim(),
            characters,
            host_character: hostCharacter,
        })
    ) {
        elements.scenarioForm.querySelector("button").disabled = true;
        elements.hostStatus.textContent = "Generating the scenario...";
    }
});

elements.endGameButton.addEventListener("click", () => {
    if (window.confirm("End this game for every player?")) {
        send("end_game", {});
    }
});

elements.retryRoundButton.addEventListener("click", () => {
    send("retry_round", {});
});

elements.startButton.addEventListener("click", () => {
    if (send("start_game", {})) {
        elements.startButton.disabled = true;
        elements.hostStatus.textContent = "Starting game...";
    }
});

elements.closeRoomButton.addEventListener("click", () => {
    if (window.confirm("Close this room for every player?")) {
        send("close_room", {});
    }
});

elements.chatForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const message = elements.chatInput.value.trim();
    if (message && send("chat", { message })) {
        elements.chatInput.value = "";
    }
});

elements.actionForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const action = elements.actionInput.value.trim();
    if (action && !elements.actionInput.disabled && send("action", { action })) {
        elements.actionInput.value = "";
        elements.actionInput.disabled = true;
    }
});

elements.skipVoteButton.addEventListener("click", () => {
    if (votedFor || elements.skipVoteButton.disabled) {
        return;
    }
    if (send("skip_vote", {})) {
        votedFor = true;
        elements.skipVoteButton.disabled = true;
    }
});

makeCharacterRow();
makeCharacterRow();
