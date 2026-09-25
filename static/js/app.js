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

function roomIdentityKey(name, inviteCode) {
    const compact = String(inviteCode || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
    return `${identityKey(name)}:${compact}`;
}

function rememberAuth(auth) {
    savedAuth = auth;
    writeStored("sessionStorage", "artificialDungeonAuth", JSON.stringify(auth));
}

function validIdentity(identity) {
    return (
        identity &&
        typeof identity.clientId === "string" &&
        /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(identity.clientId) &&
        typeof identity.reconnectToken === "string" &&
        Boolean(identity.reconnectToken)
    );
}

function rememberedIdentity(name, inviteCode) {
    // Reconnect tokens are per room: the same name in another room has a
    // different token, so prefer the room-specific record when available.
    if (inviteCode) {
        const roomIdentity = readStoredObject("localStorage", roomIdentityKey(name, inviteCode));
        if (validIdentity(roomIdentity)) {
            return roomIdentity;
        }
    }
    const identity = readStoredObject("localStorage", identityKey(name));
    return validIdentity(identity) ? identity : null;
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
    scenarioSubmit: document.getElementById("scenario-submit"),
    lobbyStep: document.getElementById("lobby-step"),
    startButton: document.getElementById("start-button"),
    editScenarioButton: document.getElementById("edit-scenario-button"),
    hostStatus: document.getElementById("host-status"),
    closeRoomButton: document.getElementById("close-room-button"),
    timeEnabled: document.getElementById("time-enabled"),
    timeConfigFields: document.getElementById("time-config-fields"),
    timeStartDay: document.getElementById("time-start-day"),
    timeStartTime: document.getElementById("time-start-time"),
    timeMaxElapsed: document.getElementById("time-max-elapsed"),
    timeDefaultElapsed: document.getElementById("time-default-elapsed"),
    timeRulesEditor: document.getElementById("time-rules-editor"),
    addTimeRule: document.getElementById("add-time-rule"),
    eventsEditor: document.getElementById("events-editor"),
    addEvent: document.getElementById("add-event"),
    lorebookEditor: document.getElementById("lorebook-editor"),
    addLorebook: document.getElementById("add-lorebook"),
    loadConfigButton: document.getElementById("load-config-button"),
    exportConfigButton: document.getElementById("export-config-button"),
    loadConfigInput: document.getElementById("load-config-input"),
    samplingTemperature: document.getElementById("sampling-temperature"),
    samplingTopP: document.getElementById("sampling-top-p"),
    promptBlocksEditor: document.getElementById("prompt-blocks-editor"),
    addPromptBlock: document.getElementById("add-prompt-block"),
    randomTurnOrder: document.getElementById("random-turn-order"),
    inviteBox: document.getElementById("invite-box"),
    inviteCode: document.getElementById("invite-code"),
    copyInvite: document.getElementById("copy-invite"),
    title: document.getElementById("scenario-title"),
    gameClock: document.getElementById("game-clock"),
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
    leaveRoomButton: document.getElementById("leave-room-button"),
    characterCardButton: document.getElementById("character-card-button"),
    characterCardModal: document.getElementById("character-card-modal"),
    characterCardForm: document.getElementById("character-card-form"),
    characterCardClose: document.getElementById("character-card-close"),
    ccDescription: document.getElementById("character-card-description"),
    ccPersonality: document.getElementById("character-card-personality"),
    ccStyle: document.getElementById("character-card-style"),
    ccExample: document.getElementById("character-card-example"),
    mobileMenuButton: document.getElementById("mobile-menu-button"),
    viewStory: document.getElementById("view-story"),
    viewChat: document.getElementById("view-chat"),
};

let authenticated = false;
let isHost = false;
let loginMode = "join";
let roomClosed = false;
let leftRoom = false;
let reconnectWhenVisible = false;
let pendingJoin = null;
let pendingRoomInfo = null;
let pendingCreateAuth = null;
let myName = "";
let amSpectator = false;
let votedFor = false;
let lastStartedRound = 0;
let oldestLoadedRound = null;
let ownCharacterCard = null;
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
    const spectators = players.filter((player) => !player.character);
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
        spectators.forEach((player) => {
            const item = document.createElement("li");
            item.className = player.connected ? "player-online" : "player-offline";
            const dot = document.createElement("span");
            dot.className = "presence-dot";
            dot.setAttribute("aria-hidden", "true");
            const name = document.createElement("span");
            name.textContent = `${player.name} · 观众`;
            const status = document.createElement("span");
            status.className = "player-presence-label";
            status.textContent = player.connected ? "Connected" : "Disconnected";
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
    elements.skipVoteBox.hidden = ownTurn || !activePlayerId || amSpectator;
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
    elements.leaveRoomButton.hidden = true;
    elements.mobileMenuButton.hidden = true;
    elements.inviteBox.hidden = true;
    elements.hostModal.hidden = true;
    elements.characterCardButton.hidden = true;
    elements.characterCardModal.hidden = true;
    ownCharacterCard = null;
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
    elements.leaveRoomButton.hidden = true;
    elements.mobileMenuButton.hidden = true;
    elements.inviteBox.hidden = true;
    elements.hostModal.hidden = true;
    elements.skipVoteBox.hidden = true;
    elements.characterCardButton.hidden = true;
    elements.characterCardModal.hidden = true;
    ownCharacterCard = null;
    elements.loginModal.hidden = false;
    elements.loginError.textContent = message;
    elements.connectionStatus.textContent = "Connected — rejoin to play";
}

function leaveRoom() {
    if (!window.confirm("Leave this room? It keeps running and you can rejoin with the invite code.")) {
        return;
    }
    closeMobileMenu();
    leftRoom = true;
    roomClosed = true;
    authenticated = false;
    isHost = false;
    amSpectator = false;
    savedAuth = null;
    writeStored("sessionStorage", "artificialDungeonAuth", null);
    for (const child of [...elements.log.children]) {
        if (child.id !== "game-banner") {
            child.remove();
        }
    }
    renderedActions.clear();
    playerColors.clear();
    lastStartedRound = 0;
    oldestLoadedRound = null;
    elements.title.textContent = "Awaiting scenario initialization...";
    elements.grid.hidden = true;
    elements.chatInput.disabled = true;
    elements.actionInput.disabled = true;
    elements.endGameButton.hidden = true;
    elements.retryRoundButton.hidden = true;
    elements.leaveRoomButton.hidden = true;
    elements.mobileMenuButton.hidden = true;
    elements.inviteBox.hidden = true;
    elements.hostModal.hidden = true;
    elements.skipVoteBox.hidden = true;
    elements.characterCardButton.hidden = true;
    elements.characterCardModal.hidden = true;
    ownCharacterCard = null;
    elements.loginModal.hidden = false;
    elements.loginError.textContent = "You left the room. It is still running; rejoin with the invite code.";
    elements.connectionStatus.textContent = "Left the room";
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.close();
    }
}

function setGameClock(label) {
    if (label) {
        elements.gameClock.hidden = false;
        elements.gameClock.textContent = label;
    } else {
        elements.gameClock.hidden = true;
        elements.gameClock.textContent = "";
    }
}

function cacheOwnCharacterCard(payload) {
    const characters = payload.characters || [];
    const mine = characters.find((char) => char.claimed_by === myName);
    ownCharacterCard = mine || null;
    elements.characterCardButton.hidden = amSpectator || !ownCharacterCard;
}

function openCharacterCard() {
    if (!ownCharacterCard) {
        return;
    }
    elements.ccDescription.value = ownCharacterCard.description || "";
    elements.ccPersonality.value = ownCharacterCard.personality || "";
    elements.ccStyle.value = ownCharacterCard.style || "";
    elements.ccExample.value = ownCharacterCard.example_dialogue || "";
    elements.characterCardModal.hidden = false;
}

function closeCharacterCard() {
    elements.characterCardModal.hidden = true;
}

elements.characterCardButton.addEventListener("click", openCharacterCard);
elements.characterCardClose.addEventListener("click", closeCharacterCard);
elements.characterCardForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (
        send("character_update", {
            description: elements.ccDescription.value.trim(),
            personality: elements.ccPersonality.value.trim(),
            style: elements.ccStyle.value.trim(),
            example_dialogue: elements.ccExample.value.trim(),
        })
    ) {
        closeCharacterCard();
    }
});

function renderHistoryRound(record) {
    if (!record || !Number.isInteger(record.round_number)) {
        return;
    }
    setPlayerOrder(record.player_order || []);
    startRound(record.round_number);
    Object.entries(record.actions || {}).forEach(([playerName, action]) => {
        showAction(record.round_number, playerName, action);
    });
    appendState(record.global_narrative, record.round_number);
    Object.entries(record.dice_results || {}).forEach(([player, roll]) => {
        appendText(
            elements.log,
            `🎲 ${player} rolled ${roll}/100`,
            `dice-entry current-round${playerColorClass(player)}`,
        );
    });
    Object.entries(record.player_resolutions || {}).forEach(([player, resolution]) => {
        appendText(
            elements.log,
            `[${player}] ${resolution}`,
            `resolution-entry current-round${playerColorClass(player)}`,
        );
    });
    markRoundComplete();
}

function historyText(text, className) {
    const entry = document.createElement("p");
    entry.className = className;
    entry.textContent = text;
    return entry;
}

function ensureHistoryAnchor() {
    let anchor = document.getElementById("earlier-rounds-anchor");
    if (anchor) {
        return anchor;
    }
    anchor = document.createElement("div");
    anchor.id = "earlier-rounds-anchor";
    const button = document.createElement("button");
    button.type = "button";
    button.id = "history-earlier-button";
    button.textContent = "Load earlier rounds";
    button.addEventListener("click", requestEarlierRounds);
    anchor.appendChild(button);
    const opening = elements.log.querySelector(".opening-scenario");
    const banner = document.getElementById("game-banner");
    (opening || banner).after(anchor);
    return anchor;
}

function requestEarlierRounds() {
    const button = document.getElementById("history-earlier-button");
    if (!button || oldestLoadedRound == null) {
        return;
    }
    button.disabled = true;
    button.textContent = "Loading...";
    send("history_request", { before_round: oldestLoadedRound });
}

function renderEarlierRounds(records) {
    const button = document.getElementById("history-earlier-button");
    if (!records.length) {
        if (button) {
            button.hidden = true;
        }
        return;
    }
    const anchor = ensureHistoryAnchor();
    const fragment = document.createDocumentFragment();
    records.forEach((record) => {
        if (!record || !Number.isInteger(record.round_number)) {
            return;
        }
        setPlayerOrder(record.player_order || []);
        fragment.appendChild(
            historyText(`Round ${record.round_number}:`, "round-heading"),
        );
        Object.entries(record.actions || {}).forEach(([playerName, action]) => {
            fragment.appendChild(
                historyText(
                    `${playerName} attempts: ${action}`,
                    `action-entry${playerColorClass(playerName)}`,
                ),
            );
        });
        const entry = document.createElement("article");
        entry.className = "state-entry";
        const label = document.createElement("strong");
        label.className = "state-round-label";
        label.textContent = `Round ${record.round_number} result`;
        const narrative = document.createElement("p");
        narrative.className = "state-narrative";
        narrative.textContent = record.global_narrative;
        entry.append(label, narrative);
        fragment.appendChild(entry);
        Object.entries(record.dice_results || {}).forEach(([player, roll]) => {
            fragment.appendChild(
                historyText(
                    `🎲 ${player} rolled ${roll}/100`,
                    `dice-entry${playerColorClass(player)}`,
                ),
            );
        });
        Object.entries(record.player_resolutions || {}).forEach(([player, resolution]) => {
            fragment.appendChild(
                historyText(
                    `[${player}] ${resolution}`,
                    `resolution-entry${playerColorClass(player)}`,
                ),
            );
        });
    });
    anchor.after(fragment);
    oldestLoadedRound = records[0].round_number;
    trimContainer(elements.log, MAX_LOG_ENTRIES);
    if (button) {
        button.disabled = false;
        button.textContent = "Load earlier rounds";
    }
}

function applySnapshot(payload) {
    isHost = payload.is_host;
    myName = payload.name;
    amSpectator = !payload.character;
    cacheOwnCharacterCard(payload);
    setGameClock(payload.game_time);
    elements.retryRoundButton.hidden = !isHost || !payload.round_paused;
    elements.actionInput.disabled = true;
    setThinking(payload.state === "AWAITING_LLM" && !payload.round_paused);
    setPlayerOrder(payload.player_order);
    renderPlayers(payload);
    const role = payload.character
        ? `(${payload.character})`
        : (isHost ? "· 主持人" : "· 观众");
    elements.identity.textContent = `${payload.name} ${role}`;
    if (payload.scenario_title) {
        elements.title.textContent = displayGameTitle(payload.scenario_title);
    }
    const emptyLog = !elements.log.querySelector(":scope > :not(#game-banner)");
    appendScenario(payload.opening_scenario);
    if (emptyLog) {
        oldestLoadedRound = null;
        const history = Array.isArray(payload.round_history) ? payload.round_history : [];
        if (history.length) {
            history.forEach(renderHistoryRound);
            oldestLoadedRound = history[0].round_number;
            const anchor = ensureHistoryAnchor();
            const button = anchor.querySelector("button");
            if (button) {
                button.hidden = false;
                button.disabled = false;
                button.textContent = "Load earlier rounds";
            }
        } else if (payload.completed_round_number && payload.scenario_state) {
            appendState(payload.scenario_state, payload.completed_round_number);
        }
    }
    if (payload.round_number) {
        startRound(payload.round_number);
        syncActions(payload.round_number, payload.submitted_actions);
    }
    if (payload.catchup && payload.catchup.missed_minutes > 0) {
        const missed = payload.catchup.missed_events.length
            ? ` Missed events: ${payload.catchup.missed_events.join(", ")}.`
            : "";
        appendText(
            elements.chatMessages,
            `You were offline from ${payload.catchup.from} to ${payload.catchup.to} (${payload.catchup.missed_minutes} minutes).${missed}`,
            "chat-entry",
            MAX_CHAT_ENTRIES,
        );
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
        const identityRecord = JSON.stringify({
            clientId,
            reconnectToken: payload.reconnect_token,
        });
        writeStored("localStorage", identityKey(payload.name), identityRecord);
        if (payload.invite_code) {
            writeStored(
                "localStorage",
                roomIdentityKey(payload.name, payload.invite_code),
                identityRecord,
            );
        }
        authenticated = true;
        roomClosed = false;
        leftRoom = false;
        elements.chatInput.disabled = false;
        elements.connectionStatus.textContent = "Connected";
        elements.loginModal.hidden = true;
        elements.grid.hidden = false;
        elements.leaveRoomButton.hidden = false;
        elements.mobileMenuButton.hidden = false;
        elements.loginError.textContent = "";
        elements.joinStep.hidden = true;
        applySnapshot(payload);
        showInviteCode(payload.invite_code);
    } else if (type === "room_info") {
        renderCharacterPicker(payload);
    } else if (type === "history_chunk") {
        renderEarlierRounds(payload.rounds || []);
        if (payload.has_more === false) {
            const button = document.getElementById("history-earlier-button");
            if (button) {
                button.hidden = true;
            }
        }
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
        if (payload.game_time !== undefined) {
            setGameClock(payload.game_time);
        }
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
        cacheOwnCharacterCard(payload);
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
        elements.scenarioSubmit.disabled = false;
    } else if (type === "error") {
        showError(payload.msg || "Unknown server error.");
        elements.scenarioSubmit.disabled = false;
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
        } else if (pendingRoomInfo) {
            const code = pendingRoomInfo;
            pendingRoomInfo = null;
            socket.send(JSON.stringify({ event_type: "room_info", data: { invite_code: code } }));
        } else if (pendingCreateAuth) {
            const auth = pendingCreateAuth;
            pendingCreateAuth = null;
            socket.send(JSON.stringify({ event_type: "create_room", data: auth }));
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
            elements.connectionStatus.textContent = leftRoom ? "Left the room" : "Disconnected";
            return;
        }
        // Do not reconnect while the page is in the background: mobile systems
        // would otherwise keep cycling the socket and reset the server's
        // departure grace period forever. Reconnect when visible again.
        if (document.hidden) {
            reconnectWhenVisible = true;
            return;
        }
        scheduleReconnect();
    });
    socket.addEventListener("error", () => {
        if (ws !== socket) return;
        elements.connectionStatus.textContent = "Connection error";
    });
}

function scheduleReconnect() {
    const delay = Math.min(1000 * (2 ** reconnectAttempts), 15000);
    reconnectAttempts += 1;
    reconnectTimer = window.setTimeout(connectSocket, delay);
}

if (document.addEventListener) {
    document.addEventListener("visibilitychange", () => {
        if (!document.hidden && reconnectWhenVisible) {
            reconnectWhenVisible = false;
            reconnectAttempts = 0;
            scheduleReconnect();
        }
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
            roomClosed = false;
            leftRoom = false;
            if (targetId !== clientId || ws.readyState !== WebSocket.OPEN) {
                clientId = targetId;
                writeStored("sessionStorage", "artificialDungeonClientId", clientId);
                pendingCreateAuth = auth;
                connectSocket();
            } else {
                send("create_room", auth);
            }
        } else {
            const inviteCode = elements.invite.value.trim();
            pendingJoin = { name, invite_code: inviteCode };
            roomClosed = false;
            leftRoom = false;
            elements.joinStep.hidden = false;
            elements.joinStatus.textContent = "Loading characters...";
            elements.joinCharacterList.replaceChildren();
            if (ws.readyState !== WebSocket.OPEN) {
                pendingRoomInfo = inviteCode;
                connectSocket();
            } else {
                send("room_info", { invite_code: inviteCode });
            }
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
    if (!characters.length) {
        elements.joinStatus.textContent = "The room has not defined its cast yet.";
        return;
    }
    elements.joinStatus.textContent = "";
    const observer = document.createElement("button");
    observer.type = "button";
    observer.className = "character-option spectator-option";
    observer.textContent = "旁观（不认领角色，仅观看）";
    observer.addEventListener("click", () => chooseCharacter(""));
    elements.joinCharacterList.appendChild(observer);
    for (const char of characters) {
        const item = document.createElement("button");
        item.type = "button";
        item.className = "character-option";
        // A character claimed by your own name remains selectable so you can
        // re-claim it after leaving; other people's claims are disabled.
        const taken = Boolean(char.claimed_by) && char.claimed_by !== pendingJoin?.name;
        if (taken) {
            item.disabled = true;
        }
        const label = char.description
            ? `${char.name} — ${char.description}`
            : char.name;
        item.textContent = char.claimed_by
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
    const identity = rememberedIdentity(pendingJoin.name, pendingJoin.invite_code);
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
    const details = document.createElement("details");
    details.className = "char-card-details";
    const summary = document.createElement("summary");
    summary.textContent = "角色卡（性格/风格/示例台词，可留空）";
    const personality = document.createElement("textarea");
    personality.className = "character-personality";
    personality.maxLength = 10000;
    personality.rows = 2;
    personality.placeholder = "性格 (personality)";
    const style = document.createElement("textarea");
    style.className = "character-style";
    style.maxLength = 10000;
    style.rows = 2;
    style.placeholder = "语言风格 (style)";
    const example = document.createElement("textarea");
    example.className = "character-example";
    example.maxLength = 10000;
    example.rows = 2;
    example.placeholder = "示例台词 (example dialogue)";
    details.append(summary, personality, style, example);
    row.append(mine, name, description, remove, details);
    elements.characterEditor.appendChild(row);
    return row;
}

function collectCharacters() {
    const rows = [...elements.characterEditor.querySelectorAll(".character-row")];
    const characters = rows.map((row) => ({
        name: row.querySelector(".character-name").value.trim(),
        description: row.querySelector(".character-description").value.trim(),
        personality: row.querySelector(".character-personality").value.trim(),
        style: row.querySelector(".character-style").value.trim(),
        example_dialogue: row.querySelector(".character-example").value.trim(),
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

function makePromptBlockRow(block = {}) {
    const row = document.createElement("div");
    row.className = "config-row prompt-block-row";
    const title = document.createElement("input");
    title.className = "prompt-block-title";
    title.maxLength = 80;
    title.placeholder = "Block title (optional)";
    title.value = block.title || "";
    const position = document.createElement("select");
    position.className = "prompt-block-position";
    ["system", "scenario", "output"].forEach((value) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        position.appendChild(option);
    });
    position.value = ["system", "scenario", "output"].includes(block.position)
        ? block.position
        : "output";
    const enabledLabel = document.createElement("label");
    enabledLabel.className = "check-label prompt-block-enabled";
    const enabledCheck = document.createElement("input");
    enabledCheck.type = "checkbox";
    enabledCheck.checked = block.enabled !== false;
    enabledLabel.append(enabledCheck, "on");
    const content = document.createElement("textarea");
    content.className = "prompt-block-content";
    content.maxLength = 10000;
    content.rows = 3;
    content.placeholder = "Instruction content";
    content.value = block.content || "";
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "remove-row";
    remove.textContent = "×";
    remove.addEventListener("click", () => row.remove());
    row.append(title, position, enabledLabel, content, remove);
    elements.promptBlocksEditor.appendChild(row);
    return row;
}

function collectPromptBlocks() {
    return [...elements.promptBlocksEditor.querySelectorAll(".prompt-block-row")]
        .map((row) => ({
            title: row.querySelector(".prompt-block-title").value.trim(),
            position: row.querySelector(".prompt-block-position").value,
            enabled: row.querySelector(".prompt-block-enabled input").checked,
            content: row.querySelector(".prompt-block-content").value.trim(),
        }))
        .filter((block) => block.content);
}

function collectSampling() {
    const sampling = {};
    const temperature = Number.parseFloat(elements.samplingTemperature.value);
    const topP = Number.parseFloat(elements.samplingTopP.value);
    if (Number.isFinite(temperature)) {
        sampling.temperature = temperature;
    }
    if (Number.isFinite(topP)) {
        sampling.top_p = topP;
    }
    return sampling;
}

elements.addPromptBlock.addEventListener("click", () => makePromptBlockRow());

function makeTimeRuleRow(activity = "", minimum = "", maximum = "") {
    const row = document.createElement("div");
    row.className = "config-row time-rule-row";
    const activityInput = document.createElement("input");
    activityInput.className = "rule-activity";
    activityInput.maxLength = 80;
    activityInput.placeholder = "Activity (e.g. searching a room)";
    activityInput.value = activity;
    const minInput = document.createElement("input");
    minInput.type = "number";
    minInput.min = 0;
    minInput.max = 525600;
    minInput.className = "rule-min";
    minInput.placeholder = "min";
    minInput.value = minimum;
    const maxInput = document.createElement("input");
    maxInput.type = "number";
    maxInput.min = 0;
    maxInput.max = 525600;
    maxInput.className = "rule-max";
    maxInput.placeholder = "max";
    maxInput.value = maximum;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "remove-row";
    remove.textContent = "×";
    remove.addEventListener("click", () => row.remove());
    row.append(activityInput, minInput, maxInput, remove);
    elements.timeRulesEditor.appendChild(row);
    return row;
}

function makeEventRow(event = {}) {
    const row = document.createElement("div");
    row.className = "config-row event-row";
    const name = document.createElement("input");
    name.className = "event-name";
    name.maxLength = 80;
    name.placeholder = "Event name";
    name.value = event.name || "";
    const day = document.createElement("input");
    day.type = "number";
    day.min = 0;
    day.max = 100000;
    day.className = "event-day";
    day.title = "Day";
    day.value = event.day ?? 1;
    const time = document.createElement("input");
    time.type = "time";
    time.className = "event-time";
    time.title = "Time";
    time.value = event.time || "06:30";
    const description = document.createElement("input");
    description.className = "event-description";
    description.maxLength = 10000;
    description.placeholder = "What happens (backstage)";
    description.value = event.description || "";
    const publicLabel = document.createElement("label");
    publicLabel.className = "check-label event-public";
    const publicCheck = document.createElement("input");
    publicCheck.type = "checkbox";
    publicCheck.checked = Boolean(event.public);
    publicCheck.title = "Announce to all players";
    publicLabel.append(publicCheck, "public");
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "remove-row";
    remove.textContent = "×";
    remove.addEventListener("click", () => row.remove());
    row.append(name, day, time, description, publicLabel, remove);
    elements.eventsEditor.appendChild(row);
    return row;
}

function makeLorebookRow(entry = {}) {
    const row = document.createElement("div");
    row.className = "config-row lorebook-row";
    const title = document.createElement("input");
    title.className = "lorebook-title";
    title.maxLength = 80;
    title.placeholder = "Title (optional)";
    title.value = entry.title || "";
    const keys = document.createElement("input");
    keys.className = "lorebook-keys";
    keys.maxLength = 1000;
    keys.placeholder = "Keywords, comma separated";
    keys.value = (entry.keys || []).join(", ");
    const content = document.createElement("textarea");
    content.className = "lorebook-content";
    content.maxLength = 10000;
    content.rows = 2;
    content.placeholder = "Content inserted when a keyword appears";
    content.value = entry.content || "";
    const order = document.createElement("input");
    order.type = "number";
    order.min = 0;
    order.max = 10000;
    order.className = "lorebook-order";
    order.title = "Insertion order (higher = stronger influence)";
    order.value = entry.order ?? 0;
    const constantLabel = document.createElement("label");
    constantLabel.className = "check-label lorebook-constant";
    const constantCheck = document.createElement("input");
    constantCheck.type = "checkbox";
    constantCheck.checked = Boolean(entry.constant);
    constantCheck.title = "Always active";
    constantLabel.append(constantCheck, "always");
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "remove-row";
    remove.textContent = "×";
    remove.addEventListener("click", () => row.remove());
    row.append(title, keys, content, order, constantLabel, remove);
    elements.lorebookEditor.appendChild(row);
    return row;
}

function collectTimeConfig() {
    if (!elements.timeEnabled.checked) {
        return { enabled: false };
    }
    const [hours, minutes] = (elements.timeStartTime.value || "06:30")
        .split(":")
        .map((part) => parseInt(part, 10) || 0);
    return {
        enabled: true,
        start_day: parseInt(elements.timeStartDay.value, 10) || 1,
        start_minute: hours * 60 + minutes,
        max_elapsed_minutes: parseInt(elements.timeMaxElapsed.value, 10) || 600,
        default_elapsed_minutes: parseInt(elements.timeDefaultElapsed.value, 10) || 15,
    };
}

function collectTimeRules() {
    return [...elements.timeRulesEditor.querySelectorAll(".time-rule-row")]
        .map((row) => ({
            activity: row.querySelector(".rule-activity").value.trim(),
            minutes_min: parseInt(row.querySelector(".rule-min").value, 10),
            minutes_max: parseInt(row.querySelector(".rule-max").value, 10),
        }))
        .filter(
            (rule) =>
                rule.activity &&
                Number.isFinite(rule.minutes_min) &&
                Number.isFinite(rule.minutes_max),
        );
}

function collectEvents() {
    if (!elements.timeEnabled.checked) {
        return [];
    }
    return [...elements.eventsEditor.querySelectorAll(".event-row")]
        .map((row) => {
            const [hours, minutes] = (row.querySelector(".event-time").value || "06:30")
                .split(":")
                .map((part) => parseInt(part, 10) || 0);
            return {
                name: row.querySelector(".event-name").value.trim(),
                day: parseInt(row.querySelector(".event-day").value, 10) || 0,
                minute: hours * 60 + minutes,
                description: row.querySelector(".event-description").value.trim(),
                public: row.querySelector(".event-public input").checked,
            };
        })
        .filter((event) => event.name && event.description);
}

function collectLorebook() {
    return [...elements.lorebookEditor.querySelectorAll(".lorebook-row")]
        .map((row) => ({
            title: row.querySelector(".lorebook-title").value.trim(),
            keys: row
                .querySelector(".lorebook-keys")
                .value.split(",")
                .map((key) => key.trim())
                .filter(Boolean),
            content: row.querySelector(".lorebook-content").value.trim(),
            order: parseInt(row.querySelector(".lorebook-order").value, 10) || 0,
            constant: row.querySelector(".lorebook-constant input").checked,
        }))
        .filter((entry) => entry.content && entry.keys.length);
}

function minutesToTime(minutes) {
    const total = Number.isFinite(minutes) ? Math.max(0, Math.min(minutes, 1439)) : 0;
    const hours = String(Math.floor(total / 60)).padStart(2, "0");
    const mins = String(total % 60).padStart(2, "0");
    return `${hours}:${mins}`;
}

function clearEditor(container) {
    container.replaceChildren();
}

function applyScenarioConfig(config) {
    if (!config || typeof config !== "object" || Array.isArray(config)) {
        throw new Error("The config file must contain a JSON object.");
    }
    // Scalar/text sections: overwrite only when the key is present in the file.
    if (typeof config.scenario === "string") {
        elements.scenario.value = config.scenario;
    }
    if (typeof config.guidance === "string") {
        elements.guidance.value = config.guidance;
    }
    if (Array.isArray(config.characters)) {
        const characters = config.characters.filter(
            (character) => character && typeof character.name === "string" && character.name.trim(),
        );
        clearEditor(elements.characterEditor);
        if (characters.length === 0) {
            makeCharacterRow();
        }
        let hostCharacter = typeof config.host_character === "string" ? config.host_character : "";
        characters.forEach((character) => {
            const row = makeCharacterRow();
            row.querySelector(".character-name").value = character.name;
            row.querySelector(".character-description").value =
                typeof character.description === "string" ? character.description : "";
            row.querySelector(".character-personality").value =
                typeof character.personality === "string" ? character.personality : "";
            row.querySelector(".character-style").value =
                typeof character.style === "string" ? character.style : "";
            row.querySelector(".character-example").value =
                typeof character.example_dialogue === "string" ? character.example_dialogue : "";
            if (hostCharacter && character.name === hostCharacter) {
                row.querySelector(".character-mine").checked = true;
                hostCharacter = "";
            }
        });
    }
    if (config.time_config && typeof config.time_config === "object") {
        const timeConfig = config.time_config;
        const enabled = Boolean(timeConfig.enabled);
        elements.timeEnabled.checked = enabled;
        elements.timeConfigFields.hidden = !enabled;
        if (enabled) {
            elements.timeStartDay.value = Number.isFinite(timeConfig.start_day)
                ? timeConfig.start_day
                : 1;
            elements.timeStartTime.value = minutesToTime(
                Number.isFinite(timeConfig.start_minute) ? timeConfig.start_minute : 390,
            );
            elements.timeMaxElapsed.value = Number.isFinite(timeConfig.max_elapsed_minutes)
                ? timeConfig.max_elapsed_minutes
                : 600;
            elements.timeDefaultElapsed.value = Number.isFinite(
                timeConfig.default_elapsed_minutes,
            )
                ? timeConfig.default_elapsed_minutes
                : 15;
        }
    }
    // List sections: append the file's rows onto what is already in the form,
    // so importing a second file no longer wipes the first file's content.
    (Array.isArray(config.time_rules) ? config.time_rules : []).forEach((rule) => {
        if (rule && typeof rule.activity === "string") {
            makeTimeRuleRow(rule.activity, rule.minutes_min ?? "", rule.minutes_max ?? "");
        }
    });
    (Array.isArray(config.events) ? config.events : []).forEach((event) => {
        if (event && typeof event.name === "string") {
            makeEventRow({
                name: event.name,
                day: event.day ?? 1,
                time: minutesToTime(event.minute),
                description: typeof event.description === "string" ? event.description : "",
                public: Boolean(event.public),
            });
        }
    });
    (Array.isArray(config.lorebook) ? config.lorebook : []).forEach((entry) => {
        if (entry && Array.isArray(entry.keys) && entry.keys.length) {
            makeLorebookRow(entry);
        }
    });
    (Array.isArray(config.prompt_blocks) ? config.prompt_blocks : []).forEach((block) => {
        if (block && typeof block.content === "string" && block.content.trim()) {
            makePromptBlockRow(block);
        }
    });
    if (config.sampling && typeof config.sampling === "object") {
        if (Number.isFinite(config.sampling.temperature)) {
            elements.samplingTemperature.value = config.sampling.temperature;
        }
        if (Number.isFinite(config.sampling.top_p)) {
            elements.samplingTopP.value = config.sampling.top_p;
        }
    }
    if (typeof config.random_turn_order === "boolean") {
        elements.randomTurnOrder.checked = config.random_turn_order;
    }
}

function exportScenarioConfig() {
    const { characters, hostCharacter } = collectCharacters();
    const config = {
        scenario: elements.scenario.value,
        guidance: elements.guidance.value,
        characters,
        host_character: hostCharacter || "",
        time_config: collectTimeConfig(),
        time_rules: collectTimeRules(),
        events: collectEvents(),
        lorebook: collectLorebook(),
        prompt_blocks: collectPromptBlocks(),
        sampling: collectSampling(),
        random_turn_order: elements.randomTurnOrder.checked,
    };
    const blob = new Blob([JSON.stringify(config, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "scenario-config.json";
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
}

elements.loadConfigButton.addEventListener("click", () => {
    elements.loadConfigInput.click();
});

elements.loadConfigInput.addEventListener("change", () => {
    const file = elements.loadConfigInput.files && elements.loadConfigInput.files[0];
    if (!file) {
        return;
    }
    const reader = new FileReader();
    reader.onload = () => {
        try {
            applyScenarioConfig(JSON.parse(String(reader.result)));
            elements.hostStatus.textContent = "Config loaded.";
        } catch (error) {
            elements.hostStatus.textContent = `Could not load config: ${error.message}`;
        }
    };
    reader.onerror = () => {
        elements.hostStatus.textContent = "Could not read the config file.";
    };
    reader.readAsText(file);
    elements.loadConfigInput.value = "";
});

elements.exportConfigButton.addEventListener("click", () => {
    exportScenarioConfig();
    elements.hostStatus.textContent = "Config exported.";
});

elements.timeEnabled.addEventListener("change", () => {
    elements.timeConfigFields.hidden = !elements.timeEnabled.checked;
});

elements.addTimeRule.addEventListener("click", () => makeTimeRuleRow());
elements.addEvent.addEventListener("click", () => makeEventRow());
elements.addLorebook.addEventListener("click", () => makeLorebookRow());
elements.editScenarioButton.addEventListener("click", () => {
    elements.lobbyStep.hidden = true;
    elements.scenarioStep.hidden = false;
    elements.hostStatus.textContent = "";
});

elements.scenarioForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const { characters, hostCharacter } = collectCharacters();
    if (characters.some((char) => !char.name)) {
        elements.hostStatus.textContent = "Every character needs a name.";
        return;
    }
    const timeConfig = collectTimeConfig();
    const events = collectEvents();
    if (events.length && !timeConfig.enabled) {
        elements.hostStatus.textContent = "Fixed-time events require the in-game clock.";
        return;
    }
    if (
        send("scenario_init", {
            scenario: elements.scenario.value.trim(),
            guidance: elements.guidance.value.trim(),
            characters,
            host_character: hostCharacter || "",
            time_config: timeConfig,
            time_rules: collectTimeRules(),
            events,
            lorebook: collectLorebook(),
            prompt_blocks: collectPromptBlocks(),
            sampling: collectSampling(),
            random_turn_order: elements.randomTurnOrder.checked,
        })
    ) {
        elements.scenarioSubmit.disabled = true;
        elements.hostStatus.textContent = "Generating the scenario...";
    }
});

function closeMobileMenu() {
    if (document.body) {
        document.body.classList.remove("menu-open");
    }
}

elements.mobileMenuButton.addEventListener("click", () => {
    if (document.body) {
        document.body.classList.toggle("menu-open");
    }
});

elements.endGameButton.addEventListener("click", () => {
    if (window.confirm("End this game for every player?")) {
        closeMobileMenu();
        send("end_game", {});
    }
});

elements.retryRoundButton.addEventListener("click", () => {
    closeMobileMenu();
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

elements.leaveRoomButton.addEventListener("click", leaveRoom);

function setMobileView(view) {
    elements.grid.classList.toggle("view-chat", view === "chat");
    elements.viewStory.classList.toggle("active", view === "story");
    elements.viewChat.classList.toggle("active", view === "chat");
}

elements.viewStory.addEventListener("click", () => setMobileView("story"));
elements.viewChat.addEventListener("click", () => setMobileView("chat"));

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
