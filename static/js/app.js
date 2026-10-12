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
    llmFields: document.getElementById("llm-fields"),
    llmModeOwn: document.getElementById("llm-mode-own"),
    llmModeBorrow: document.getElementById("llm-mode-borrow"),
    llmOwnFields: document.getElementById("llm-own-fields"),
    apiKey: document.getElementById("api-key-input"),
    llmProvider: document.getElementById("llm-provider"),
    llmEndpoint: document.getElementById("llm-endpoint"),
    llmModel: document.getElementById("llm-model"),
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
    opening: document.getElementById("opening-input"),
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
    importStButton: document.getElementById("import-st-button"),
    importStInput: document.getElementById("import-st-input"),
    importWarnings: document.getElementById("import-warnings"),
    samplingTemperature: document.getElementById("sampling-temperature"),
    samplingTopP: document.getElementById("sampling-top-p"),
    samplingFrequencyPenalty: document.getElementById("sampling-frequency-penalty"),
    samplingPresencePenalty: document.getElementById("sampling-presence-penalty"),
    samplingTopK: document.getElementById("sampling-top-k"),
    samplingMinP: document.getElementById("sampling-min-p"),
    samplingRepetitionPenalty: document.getElementById("sampling-repetition-penalty"),
    promptBlocksEditor: document.getElementById("prompt-blocks-editor"),
    addPromptBlock: document.getElementById("add-prompt-block"),
    truthPluginEnabled: document.getElementById("truth-plugin-enabled"),
    truthPluginFields: document.getElementById("truth-plugin-fields"),
    truthMaxChars: document.getElementById("truth-max-chars"),
    truthRequireDeclarative: document.getElementById("truth-require-declarative"),
    truthFeedDm: document.getElementById("truth-feed-dm"),
    truthQuestionsEditor: document.getElementById("truth-questions-editor"),
    addTruthQuestion: document.getElementById("add-truth-question"),
    worldStartRegion: document.getElementById("world-start-region"),
    regionsEditor: document.getElementById("regions-editor"),
    addRegion: document.getElementById("add-region"),
    itemsEditor: document.getElementById("items-editor"),
    addItem: document.getElementById("add-item"),
    randomTurnOrder: document.getElementById("random-turn-order"),
    scheduleStrict: document.getElementById("schedule-strict"),
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
    scheduleStatus: document.getElementById("schedule-status"),
    holdButton: document.getElementById("hold-button"),
    dmThinking: document.getElementById("dm-thinking"),
    connectionStatus: document.getElementById("connection-status"),
    tokenUsage: document.getElementById("token-usage"),
    tokenChart: document.getElementById("token-chart"),
    tokenCount: document.getElementById("token-count"),
    endGameButton: document.getElementById("end-game-button"),
    retryRoundButton: document.getElementById("retry-round-button"),
    leaveRoomButton: document.getElementById("leave-room-button"),
    characterCardButton: document.getElementById("character-card-button"),
    mapButton: document.getElementById("map-button"),
    mapModal: document.getElementById("map-modal"),
    mapBody: document.getElementById("map-body"),
    mapClose: document.getElementById("map-close"),
    codexButton: document.getElementById("codex-button"),
    codexModal: document.getElementById("codex-modal"),
    codexBody: document.getElementById("codex-body"),
    codexClose: document.getElementById("codex-close"),
    characterCardModal: document.getElementById("character-card-modal"),    characterCardForm: document.getElementById("character-card-form"),
    characterCardClose: document.getElementById("character-card-close"),
    ccDescription: document.getElementById("character-card-description"),
    ccPersonality: document.getElementById("character-card-personality"),
    ccStyle: document.getElementById("character-card-style"),
    ccExample: document.getElementById("character-card-example"),
    truthButton: document.getElementById("truth-button"),
    truthModal: document.getElementById("truth-modal"),
    truthQuestions: document.getElementById("truth-questions"),
    truthRoundStatus: document.getElementById("truth-round-status"),
    truthError: document.getElementById("truth-error"),
    truthClose: document.getElementById("truth-close"),
    keyRequiredModal: document.getElementById("key-required-modal"),
    keyRequiredForm: document.getElementById("key-required-form"),
    keyRequiredInput: document.getElementById("key-required-input"),
    keyRequiredError: document.getElementById("key-required-error"),
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
let truthState = null;
let roomState = null;
let strictMode = false;
let activePlayerIds = [];
let hasMap = false;
let mapSnapshot = null;
let hasCodex = false;
let codexSnapshot = null;
let lastSectionLabel = null;
const renderedActions = new Set();
const playerColors = new Map();
const MAX_LOG_ENTRIES = 2000;
const MAX_CHAT_ENTRIES = 1000;

function send(eventType, data) {
    if (ws.readyState !== WebSocket.OPEN) {
        showError("服务器连接未建立。");
        return false;
    }
    ws.send(JSON.stringify({ event_type: eventType, data }));
    return true;
}

function trimContainer(container, maximum) {
    while (container.children.length > maximum) {
        const removed = container.firstElementChild?.id === "game-banner"
            ? container.firstElementChild.nextElementSibling : container.firstElementChild;
        if (removed) {
            if (removed.dataset && removed.dataset.actionKey) {
                renderedActions.delete(removed.dataset.actionKey);
            }
            if (removed.querySelectorAll) {
                removed.querySelectorAll("[data-action-key]").forEach((node) => {
                    renderedActions.delete(node.dataset.actionKey);
                });
            }
            removed.remove();
        }
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

let currentRoundBlock = null;

function beginRoundBlock(title) {
    elements.log.querySelectorAll(".round-block.current-round").forEach((block) => {
        block.classList.remove("current-round");
    });
    const block = document.createElement("section");
    block.className = "round-block current-round";
    const heading = document.createElement("h3");
    heading.className = "round-title";
    heading.textContent = title;
    block.appendChild(heading);
    elements.log.appendChild(block);
    currentRoundBlock = block;
    trimContainer(elements.log, MAX_LOG_ENTRIES);
    elements.log.scrollTop = elements.log.scrollHeight;
    return block;
}

function ensureRoundBlock() {
    if (!currentRoundBlock) {
        beginRoundBlock("剧情");
    }
    return currentRoundBlock;
}

function startRound(roundNumber) {
    if (!Number.isInteger(roundNumber) || roundNumber <= lastStartedRound) {
        return;
    }
    lastStartedRound = roundNumber;
    beginRoundBlock(`第 ${roundNumber} 回合`);
}

function startTimeSection(label) {
    if (!label || label === lastSectionLabel) {
        return;
    }
    lastSectionLabel = label;
    beginRoundBlock(label);
}

function markRoundComplete() {
    elements.log.querySelectorAll(".round-block.latest-complete-round").forEach((block) => {
        block.classList.remove("latest-complete-round");
    });
    if (currentRoundBlock) {
        currentRoundBlock.classList.add("latest-complete-round");
    }
}

function declarationLine(playerName, action) {
    const line = document.createElement("p");
    line.className = `declaration${playerColorClass(playerName)}`;
    const name = document.createElement("span");
    name.className = "declaration-name";
    name.textContent = playerName;
    const text = document.createElement("span");
    text.className = "declaration-text";
    text.textContent = action;
    line.append(name, text);
    return line;
}

function sceneBlock(text) {
    const scene = document.createElement("div");
    scene.className = "scene-block";
    const paragraph = document.createElement("p");
    paragraph.className = "scene-text";
    paragraph.textContent = text;
    scene.appendChild(paragraph);
    return scene;
}

function diceRow(pairs) {
    const row = document.createElement("div");
    row.className = "dice-row";
    pairs.forEach(([player, roll]) => {
        const chip = document.createElement("span");
        chip.className = `dice-chip${playerColorClass(player)}`;
        chip.textContent = `🎲 ${player} ${roll}/100`;
        row.appendChild(chip);
    });
    return row;
}

function outcomesWrap(pairs) {
    const wrap = document.createElement("div");
    wrap.className = "outcomes";
    pairs.forEach(([player, text]) => {
        const card = document.createElement("article");
        card.className = `outcome${playerColorClass(player)}`;
        const name = document.createElement("div");
        name.className = "outcome-name";
        name.textContent = player;
        const body = document.createElement("p");
        body.className = "outcome-text";
        body.textContent = text;
        card.append(name, body);
        wrap.appendChild(card);
    });
    return wrap;
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
    elements.lobbyPlayerCount.textContent = `在线 ${connected}/${players.length} 人`;
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
                ? (char.connected ? "在线" : "离线")
                : "未认领";
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
            status.textContent = player.connected ? "在线" : "离线";
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
    const block = ensureRoundBlock();
    let wrap = block.querySelector(".declarations");
    if (!wrap) {
        wrap = document.createElement("div");
        wrap.className = "declarations";
        block.appendChild(wrap);
    }
    const line = declarationLine(playerName, action);
    line.dataset.actionKey = actionKey;
    wrap.appendChild(line);
    elements.log.scrollTop = elements.log.scrollHeight;
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
    entry.className = "opening-entry opening-scenario";
    const label = document.createElement("strong");
    label.className = "opening-label";
    label.textContent = "开场剧情";
    const narrative = document.createElement("p");
    narrative.className = "opening-text";
    narrative.textContent = scenario;
    entry.append(label, narrative);
    const anchor = document.getElementById("game-banner");
    anchor.after(entry);
    trimContainer(elements.log, MAX_LOG_ENTRIES);
}

function appendState(text, roundNumber = null) {
    if (!text) {
        return;
    }
    const block = ensureRoundBlock();
    if (block.querySelector(".scene-block")) {
        return;
    }
    block.appendChild(sceneBlock(text));
    trimContainer(elements.log, MAX_LOG_ENTRIES);
    elements.log.scrollTop = elements.log.scrollHeight;
}

function appendSceneNote(text) {
    if (!text) {
        return;
    }
    const block = ensureRoundBlock();
    const note = document.createElement("p");
    note.className = "scene-note";
    note.textContent = text;
    block.appendChild(note);
    elements.log.scrollTop = elements.log.scrollHeight;
}

function appendDice(pairs) {
    if (!pairs.length) {
        return;
    }
    ensureRoundBlock().appendChild(diceRow(pairs));
}

function appendOutcomes(pairs) {
    if (!pairs.length) {
        return;
    }
    ensureRoundBlock().appendChild(outcomesWrap(pairs));
}

function showError(message) {
    if (!authenticated) {
        elements.loginModal.hidden = false;
        elements.loginError.textContent = message;
    } else {
        appendText(
            elements.chatMessages,
            `错误：${message}`,
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
        ? "输入你的行动…"
        : `等待 ${activePlayerName || "下一位玩家"}…`;
    if (ownTurn) {
        elements.actionInput.focus();
    }
    updateSkipVote(activePlayerId);
}

function applyScheduleWindow(payload) {
    strictMode = true;
    roomState = "ACTIVE_TURN";
    activePlayerIds = payload.active_player_ids || [];
    updateTruthButton();
    if (payload.game_time !== undefined) {
        setGameClock(payload.game_time);
    }
    if (payload.player_order) {
        setPlayerOrder(payload.player_order);
    }
    if (elements.skipVoteBox) {
        elements.skipVoteBox.hidden = true;
    }
    const own = activePlayerIds.includes(clientId);
    if (elements.holdButton) {
        elements.holdButton.hidden = !own;
    }
    elements.actionInput.disabled = !own;
    elements.actionInput.placeholder = own ? "输入你的行动…" : "等待其他玩家…";
    if (own) {
        elements.actionInput.focus();
    }
    if (!elements.scheduleStatus) {
        return;
    }
    const waiting = payload.waiting || [];
    const lines = waiting.map((item) => item.action
        ? `${item.name} 仍在${item.action}（预计 ${item.ends_at_label}）`
        : `${item.name} 正在观望`);
    const selfWait = waiting.find((item) => item.player_id === clientId);
    elements.scheduleStatus.replaceChildren();
    const text = document.createElement("div");
    if (selfWait && !own) {
        text.textContent = selfWait.action
            ? `你正在${selfWait.action}，预计 ${selfWait.ends_at_label} 完成`
            : "你正在观望…";
    } else {
        text.textContent = lines.join("；");
    }
    elements.scheduleStatus.appendChild(text);
    const peerRow = document.createElement("div");
    peerRow.className = "skip-row";
    const ids = payload.active_player_ids || [];
    const names = payload.active_player_names || [];
    ids.forEach((id, index) => {
        if (id === clientId) {
            return;
        }
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = `跳过 ${names[index] || id}`;
        button.addEventListener("click", () => send("skip_vote", { target: id }));
        peerRow.appendChild(button);
    });
    if (peerRow.children.length) {
        elements.scheduleStatus.appendChild(peerRow);
    }
    elements.scheduleStatus.hidden = text.textContent === "" && peerRow.children.length === 0;
}

function holdTurn() {
    if (send("hold", {})) {
        if (elements.holdButton) {
            elements.holdButton.hidden = true;
        }
        elements.actionInput.disabled = true;
    }
}

function updateMapButton() {
    if (elements.mapButton) {
        elements.mapButton.hidden = !hasMap || !authenticated;
    }
}

function updateCodexButton() {
    if (elements.codexButton) {
        elements.codexButton.hidden = !hasCodex || !authenticated;
    }
}

function openMap() {
    if (!hasMap || !elements.mapModal) {
        return;
    }
    elements.mapModal.hidden = false;
    renderMap();
    send("map_request", {});
}

function closeMap() {
    if (elements.mapModal) {
        elements.mapModal.hidden = true;
    }
}

function mapChip(text, extraClass) {
    const chip = document.createElement("span");
    chip.className = extraClass ? `map-chip ${extraClass}` : "map-chip";
    chip.textContent = text;
    return chip;
}

function mapBlock(label, nodes) {
    const block = document.createElement("div");
    block.className = "map-block";
    const caption = document.createElement("span");
    caption.className = "map-block-label";
    caption.textContent = label;
    block.appendChild(caption);
    const content = document.createElement("div");
    content.className = "map-block-content";
    nodes.forEach((node) => content.appendChild(node));
    block.appendChild(content);
    return block;
}

function renderMap() {
    const body = elements.mapBody;
    if (!body) {
        return;
    }
    body.replaceChildren();
    if (!mapSnapshot) {
        const hint = document.createElement("p");
        hint.className = "map-empty";
        hint.textContent = "正在读取地图…";
        body.appendChild(hint);
        return;
    }
    const regions = mapSnapshot.regions || [];
    const players = mapSnapshot.players || [];
    const regionNames = new Map(regions.map((region) => [region.id, region.name]));
    const myCharacter = ownCharacterCard ? ownCharacterCard.name : null;

    // Team positions and packs.
    const teams = document.createElement("section");
    teams.className = "map-section";
    const teamsHeading = document.createElement("h3");
    teamsHeading.textContent = "队伍";
    teams.appendChild(teamsHeading);
    const teamList = document.createElement("div");
    teamList.className = "map-team-list";
    if (!players.length) {
        const empty = document.createElement("p");
        empty.className = "map-empty";
        empty.textContent = "暂无角色";
        teamList.appendChild(empty);
    }
    players.forEach((player) => {
        const row = document.createElement("div");
        row.className = "map-team";
        const name = document.createElement("span");
        name.className = "map-team-name";
        name.textContent = player.name;
        row.appendChild(name);
        const loc = document.createElement("span");
        loc.className = "map-team-loc";
        if (player.in_transit_to) {
            loc.classList.add("in-transit");
            loc.textContent = `在途 → ${regionNames.get(player.in_transit_to) || player.in_transit_to}`;
        } else {
            loc.textContent = regionNames.get(player.region) || player.region || "未知";
        }
        row.appendChild(loc);
        const pack = document.createElement("span");
        pack.className = "map-team-pack";
        const carried = player.carried || [];
        pack.textContent = carried.length ? `携带：${carried.join("、")}` : "携带：（空）";
        row.appendChild(pack);
        teamList.appendChild(row);
    });
    teams.appendChild(teamList);
    body.appendChild(teams);

    // Regions, occupied ones first.
    if (regions.length) {
        const section = document.createElement("section");
        section.className = "map-section";
        const heading = document.createElement("h3");
        heading.textContent = "地区";
        section.appendChild(heading);
        const grid = document.createElement("div");
        grid.className = "map-grid";
        const occupiedCount = (region) =>
            players.filter((player) => player.region === region.id).length;
        [...regions]
            .sort((a, b) => occupiedCount(b) - occupiedCount(a))
            .forEach((region) => {
                const card = document.createElement("article");
                card.className = "map-region";
                const occupants = players.filter((player) => player.region === region.id);
                if (myCharacter && occupants.some((player) => player.name === myCharacter)) {
                    card.classList.add("map-here");
                }
                const title = document.createElement("h4");
                title.textContent = region.name;
                card.appendChild(title);
                const occ = document.createElement("p");
                occ.className = "map-occupants";
                occ.textContent = occupants.length
                    ? "在场：" + occupants.map((player) => player.name).join("、")
                    : "无人在此";
                card.appendChild(occ);

                const items = region.items || [];
                if (items.length) {
                    card.appendChild(
                        mapBlock(
                            "物品",
                            items.map((item) => {
                                const line = document.createElement("div");
                                line.className = "map-item";
                                const label = document.createElement("span");
                                label.className = "map-item-name";
                                label.textContent = item.name;
                                line.appendChild(label);
                                if (item.clue) {
                                    line.appendChild(mapChip("线索", "clue"));
                                }
                                if (item.carryable === false) {
                                    line.appendChild(mapChip("不可携带", "locked"));
                                }
                                if (item.investigated) {
                                    line.appendChild(mapChip("已调查", "done"));
                                }
                                if (item.summary) {
                                    const summary = document.createElement("span");
                                    summary.className = "map-item-summary";
                                    summary.textContent = item.summary;
                                    line.appendChild(summary);
                                }
                                if (item.investigated && item.detail) {
                                    const detail = document.createElement("div");
                                    detail.className = "map-item-detail";
                                    detail.textContent = item.detail;
                                    line.appendChild(detail);
                                }
                                return line;
                            }),
                        ),
                    );
                }
                const reachable = region.reachable || [];
                if (reachable.length) {
                    card.appendChild(
                        mapBlock(
                            "可前往",
                            reachable.map((target) => mapChip(target.name)),
                        ),
                    );
                }
                const locked = region.locked || [];
                if (locked.length) {
                    card.appendChild(
                        mapBlock(
                            "需条件",
                            locked.map((target) => mapChip(target.name, "locked")),
                        ),
                    );
                }
                grid.appendChild(card);
            });
        section.appendChild(grid);
        body.appendChild(section);
    }
}

function openCodex() {
    if (!hasCodex || !elements.codexModal) {
        return;
    }
    elements.codexModal.hidden = false;
    renderCodex();
    send("codex_request", {});
}

function closeCodex() {
    if (elements.codexModal) {
        elements.codexModal.hidden = true;
    }
}

function renderCodex() {
    const body = elements.codexBody;
    if (!body) {
        return;
    }
    body.replaceChildren();
    if (!codexSnapshot) {
        const hint = document.createElement("p");
        hint.className = "map-empty";
        hint.textContent = "正在读取…";
        body.appendChild(hint);
        return;
    }
    const itemsSection = document.createElement("section");
    itemsSection.className = "map-section";
    const itemsTitle = document.createElement("h3");
    itemsTitle.textContent = "已发现的物品";
    itemsSection.appendChild(itemsTitle);
    const items = codexSnapshot.items || [];
    if (!items.length) {
        const empty = document.createElement("p");
        empty.className = "map-empty";
        empty.textContent = "尚未发现任何物品。";
        itemsSection.appendChild(empty);
    } else {
        const grid = document.createElement("div");
        grid.className = "map-grid";
        items.forEach((item) => {
            const card = document.createElement("article");
            card.className = "map-region";
            const name = document.createElement("h4");
            name.textContent = item.name;
            card.appendChild(name);
            const tags = [];
            if (item.clue) {
                tags.push("线索");
            }
            if (item.investigated) {
                tags.push("已调查");
            }
            if (item.here) {
                tags.push("随身携带");
            }
            const meta = document.createElement("p");
            meta.className = "map-occupants";
            const origin = item.origin ? `原本所在地：${item.origin}` : "";
            meta.textContent = [origin, tags.join("·")].filter(Boolean).join(" · ");
            card.appendChild(meta);
            if (item.summary) {
                const intro = document.createElement("p");
                intro.className = "map-item-summary";
                intro.textContent = item.summary;
                card.appendChild(intro);
            }
            if (item.investigated && item.detail) {
                const detail = document.createElement("div");
                detail.className = "map-item-detail";
                detail.textContent = item.detail;
                card.appendChild(detail);
            }
            grid.appendChild(card);
        });
        itemsSection.appendChild(grid);
    }
    body.appendChild(itemsSection);

    const eventsSection = document.createElement("section");
    eventsSection.className = "map-section";
    const eventsTitle = document.createElement("h3");
    eventsTitle.textContent = "已触发的事件";
    eventsSection.appendChild(eventsTitle);
    const events = codexSnapshot.events || [];
    if (!events.length) {
        const empty = document.createElement("p");
        empty.className = "map-empty";
        empty.textContent = "尚未触发任何事件。";
        eventsSection.appendChild(empty);
    } else {
        events.forEach((event) => {
            const card = document.createElement("article");
            card.className = "map-region";
            const name = document.createElement("h4");
            name.textContent = event.actor ? `${event.name}（${event.actor}）` : event.name;
            card.appendChild(name);
            if (event.region) {
                const where = document.createElement("p");
                where.className = "map-occupants";
                where.textContent = `发生于：${event.region}`;
                card.appendChild(where);
            }
            const desc = document.createElement("p");
            desc.className = "map-item-summary";
            desc.textContent = event.description;
            card.appendChild(desc);
            eventsSection.appendChild(card);
        });
    }
    body.appendChild(eventsSection);
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
    roomState = null;
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
    elements.keyRequiredModal.hidden = true;
    hideTruthUI();
    strictMode = false;
    activePlayerIds = [];
    elements.holdButton.hidden = true;
    elements.scheduleStatus.hidden = true;
    hasMap = false;
    mapSnapshot = null;
    elements.mapButton.hidden = true;
    elements.mapModal.hidden = true;
    hasCodex = false;
    codexSnapshot = null;
    elements.codexButton.hidden = true;
    elements.codexModal.hidden = true;
    ownCharacterCard = null;
    elements.loginModal.hidden = false;
    elements.loginError.textContent = message;
    elements.connectionStatus.textContent = "离线";
}

function wasRemoved(message) {
    authenticated = false;
    isHost = false;
    roomState = null;
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
    elements.keyRequiredModal.hidden = true;
    hideTruthUI();
    strictMode = false;
    activePlayerIds = [];
    elements.holdButton.hidden = true;
    elements.scheduleStatus.hidden = true;
    hasMap = false;
    mapSnapshot = null;
    elements.mapButton.hidden = true;
    elements.mapModal.hidden = true;
    hasCodex = false;
    codexSnapshot = null;
    elements.codexButton.hidden = true;
    elements.codexModal.hidden = true;
    ownCharacterCard = null;
    elements.loginModal.hidden = false;
    elements.loginError.textContent = message;
    elements.connectionStatus.textContent = "已连接——重新加入即可继续游戏";
}

function leaveRoom() {
    if (!window.confirm("离开该房间？房间会继续运行，你可以用邀请码重新加入。")) {
        return;
    }
    closeMobileMenu();
    leftRoom = true;
    roomClosed = true;
    authenticated = false;
    isHost = false;
    amSpectator = false;
    roomState = null;
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
    elements.title.textContent = "等待剧情初始化…";
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
    elements.keyRequiredModal.hidden = true;
    hideTruthUI();
    strictMode = false;
    activePlayerIds = [];
    elements.holdButton.hidden = true;
    elements.scheduleStatus.hidden = true;
    hasMap = false;
    mapSnapshot = null;
    elements.mapButton.hidden = true;
    elements.mapModal.hidden = true;
    hasCodex = false;
    codexSnapshot = null;
    elements.codexButton.hidden = true;
    elements.codexModal.hidden = true;
    ownCharacterCard = null;
    elements.loginModal.hidden = false;
    elements.loginError.textContent = "你已离开房间，它仍在运行，可用邀请码重新加入。";
    elements.connectionStatus.textContent = "已离开房间";
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

function rememberOwnCharacter(payload) {
    // The client only learns its claimed character after the host claims it or a
    // roster update arrives; keep the reconnect token's character in sync so a
    // reconnect does not look like an intentional switch to spectating.
    if (!savedAuth || !myName) {
        return;
    }
    const characters = payload.characters || [];
    const mine = characters.find((char) => char.claimed_by === myName);
    const next = mine ? mine.name : null;
    if (savedAuth.character !== next) {
        savedAuth.character = next;
        writeStored("sessionStorage", "artificialDungeonAuth", JSON.stringify(savedAuth));
    }
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

function applyTruthState(state) {
    truthState = state && typeof state === "object" ? state : null;
    if (!elements.truthButton) {
        return;
    }
    elements.truthButton.classList.remove("truth-new");
    updateTruthButton();
    if (elements.truthModal && !elements.truthModal.hidden) {
        renderTruth();
    }
}

function updateTruthButton() {
    if (!elements.truthButton) {
        return;
    }
    const active = roomState === "ACTIVE_TURN" || roomState === "AWAITING_LLM";
    elements.truthButton.hidden = !(truthState && truthState.enabled && active && !leftRoom);
}

function truthRoundUsed() {
    return Boolean(truthState && truthState.your_used_round === truthState.round);
}

function updateTruthRoundStatus() {
    if (!truthState || !truthState.enabled) {
        elements.truthRoundStatus.textContent = "本房间未启用『可能的真相』。";
    } else if (!(truthState.questions || []).length) {
        elements.truthRoundStatus.textContent = "还没有揭晓的提问，请等待剧情推进。";
    } else if (amSpectator) {
        elements.truthRoundStatus.textContent = "你正在旁观，选择角色后才能提交真相。";
    } else if (truthRoundUsed()) {
        elements.truthRoundStatus.textContent =
            `第 ${truthState.round} 回合：你已提交过（每轮一次）。`;
    } else {
        elements.truthRoundStatus.textContent =
            `第 ${truthState.round} 回合：选择一个提问提交你的真相（每轮一次）。`;
    }
}

function renderTruth() {
    elements.truthError.textContent = "";
    elements.truthQuestions.replaceChildren();
    updateTruthRoundStatus();
    if (!truthState || !truthState.enabled) {
        return;
    }
    const locked = amSpectator || truthRoundUsed();
    (truthState.questions || []).forEach((question) => {
        const article = document.createElement("article");
        article.className = "truth-question";
        const title = document.createElement("h3");
        title.textContent = question.text;
        article.appendChild(title);
        const history = document.createElement("ul");
        history.className = "truth-history";
        const entries = question.history || [];
        if (entries.length) {
            entries.forEach((entry) => {
                const item = document.createElement("li");
                item.textContent = `${entry.character}：${entry.text}`;
                history.appendChild(item);
            });
        } else {
            const item = document.createElement("li");
            item.className = "truth-empty";
            item.textContent = "（暂无被采纳的真相）";
            history.appendChild(item);
        }
        article.appendChild(history);
        const form = document.createElement("form");
        form.className = "truth-form";
        form.dataset.questionId = question.id;
        const input = document.createElement("textarea");
        input.maxLength = truthState.max_answer_chars || 500;
        input.rows = 2;
        input.placeholder = "以陈述句写下你的真相或片段…";
        input.disabled = locked;
        const submit = document.createElement("button");
        submit.type = "submit";
        submit.textContent = "提交真相";
        submit.disabled = locked;
        form.append(input, submit);
        form.addEventListener("submit", (event) => {
            event.preventDefault();
            const answer = input.value.trim();
            if (!answer || answer.length > (truthState.max_answer_chars || 500)) {
                elements.truthError.textContent =
                    `真相内容不能为空，且不能超过 ${truthState.max_answer_chars || 500} 字。`;
                return;
            }
            if (send("truth_submit", { question_id: question.id, answer })) {
                input.disabled = true;
                submit.disabled = true;
                submit.textContent = "判定中…";
                elements.truthError.textContent = "正在判定你的真相…";
            }
        });
        article.appendChild(form);
        elements.truthQuestions.appendChild(article);
    });
}

function openTruth() {
    elements.truthModal.hidden = false;
    elements.truthButton.classList.remove("truth-new");
    renderTruth();
    send("truth_request", {});
}

function closeTruth() {
    elements.truthModal.hidden = true;
}

function hideTruthUI() {
    truthState = null;
    elements.truthButton.hidden = true;
    elements.truthButton.classList.remove("truth-new");
    elements.truthModal.hidden = true;
}

elements.truthButton.addEventListener("click", openTruth);
elements.truthClose.addEventListener("click", closeTruth);
elements.mapButton.addEventListener("click", openMap);
elements.mapClose.addEventListener("click", closeMap);
elements.codexButton.addEventListener("click", openCodex);
elements.codexClose.addEventListener("click", closeCodex);

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

function buildHistoryBlock(record) {
    const block = document.createElement("section");
    block.className = "round-block";
    const heading = document.createElement("h3");
    heading.className = "round-title";
    heading.textContent = `第 ${record.round_number} 回合`;
    block.appendChild(heading);
    const actions = Object.entries(record.actions || {});
    if (actions.length) {
        const wrap = document.createElement("div");
        wrap.className = "declarations";
        actions.forEach(([playerName, action]) => {
            wrap.appendChild(declarationLine(playerName, action));
        });
        block.appendChild(wrap);
    }
    if (record.global_narrative) {
        block.appendChild(sceneBlock(record.global_narrative));
    }
    const dice = Object.entries(record.dice_results || {});
    if (dice.length) {
        block.appendChild(diceRow(dice));
    }
    const outcomes = Object.entries(record.player_resolutions || {});
    if (outcomes.length) {
        block.appendChild(outcomesWrap(outcomes));
    }
    return block;
}

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
    appendDice(Object.entries(record.dice_results || {}));
    appendOutcomes(Object.entries(record.player_resolutions || {}));
    markRoundComplete();
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
    button.textContent = "加载更早的回合";
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
    button.textContent = "加载中…";
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
        fragment.appendChild(buildHistoryBlock(record));
    });
    anchor.after(fragment);
    oldestLoadedRound = records[0].round_number;
    trimContainer(elements.log, MAX_LOG_ENTRIES);
    if (button) {
        button.disabled = false;
        button.textContent = "加载更早的回合";
    }
}

function applySnapshot(payload) {
    isHost = payload.is_host;
    myName = payload.name;
    amSpectator = !payload.character;
    roomState = payload.state;
    strictMode = payload.schedule_mode === "strict";
    hasMap = Boolean(payload.has_map);
    hasCodex = Boolean(payload.has_codex);
    mapSnapshot = null;
    codexSnapshot = null;
    lastSectionLabel = null;
    updateMapButton();
    updateCodexButton();
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
                button.textContent = "加载更早的回合";
            }
        } else if (payload.completed_round_number && payload.scenario_state) {
            appendState(payload.scenario_state, payload.completed_round_number);
        }
    }
    if (payload.round_number) {
        if (strictMode && payload.game_time) {
            startTimeSection(payload.game_time);
        } else {
            startRound(payload.round_number);
        }
        syncActions(payload.round_number, payload.submitted_actions);
    }
    if (payload.catchup && payload.catchup.missed_minutes > 0) {
        const missed = payload.catchup.missed_events.length
            ? ` 错过的全局事件：${payload.catchup.missed_events.join(", ")}。`
            : "";
        appendText(
            elements.chatMessages,
            `你在 ${payload.catchup.from} 到 ${payload.catchup.to} 期间离线（游戏内 ${payload.catchup.missed_minutes} 分钟）。${missed}`,
            "chat-entry",
            MAX_CHAT_ENTRIES,
        );
    }
    if (payload.state === "ACTIVE_TURN") {
        applyTurn(payload.active_player_id, payload.active_player_name);
    }
    if (payload.schedule) {
        applyScheduleWindow(payload.schedule);
    }
    showHostStep(payload.state);
    elements.endGameButton.hidden = !isHost || !["ACTIVE_TURN", "AWAITING_LLM"].includes(payload.state);
    applyTruthState(payload.truth);
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
        elements.connectionStatus.textContent = "在线";
        elements.loginModal.hidden = true;
        elements.grid.hidden = false;
        elements.leaveRoomButton.hidden = false;
        elements.mobileMenuButton.hidden = false;
        elements.loginError.textContent = "";
        elements.joinStep.hidden = true;
        applySnapshot(payload);
        rememberOwnCharacter(payload);
        showInviteCode(payload.invite_code);
        if (payload.needs_key && isHost) {
            elements.keyRequiredModal.hidden = false;
        }
    } else if (type === "key_updated") {
        elements.keyRequiredModal.hidden = true;
        elements.keyRequiredError.textContent = "";
        appendText(
            elements.chatMessages,
            `系统：${payload.msg || "API Key 已更新。"}`,
            "chat-entry",
            MAX_CHAT_ENTRIES,
        );
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
    } else if (type === "turn_window") {
        setThinking(false);
        strictMode = true;
        startTimeSection(payload.game_time);
        applyScheduleWindow(payload);
    } else if (type === "turn_directive") {
        startRound(payload.round_number);
        syncActions(payload.round_number, payload.submitted_actions);
        applyTurn(payload.active_player_id, payload.active_player_name);
    } else if (type === "round_start") {
        if (!strictMode) {
            startRound(payload.round_number);
        }
    } else if (type === "scene_text") {
        appendSceneNote(payload.text);
    } else if (type === "map_state") {
        mapSnapshot = payload;
        if (!elements.mapModal.hidden) {
            renderMap();
        }
    } else if (type === "codex_state") {
        codexSnapshot = payload;
        if (!elements.codexModal.hidden) {
            renderCodex();
        }
    } else if (type === "action_echo") {
        showAction(
            payload.round_number,
            payload.player_name,
            payload.action,
            payload.player_color_index,
        );
    } else if (type === "state_update") {
        setThinking(false);
        roomState = "ACTIVE_TURN";
        updateTruthButton();
        setPlayerOrder(payload.player_order);
        if (payload.game_time !== undefined) {
            setGameClock(payload.game_time);
        }
        if (payload.round_title) {
            elements.title.textContent = displayGameTitle(payload.round_title);
        }
        if (strictMode) {
            startTimeSection(payload.game_time);
        } else {
            startRound(payload.round_number);
        }
        syncActions(payload.round_number, payload.submitted_actions);
        appendState(payload.global_narrative, payload.round_number);
        appendDice(Object.entries(payload.dice_results || {}));
        appendOutcomes(Object.entries(payload.player_resolutions || {}));
        markRoundComplete();
    } else if (type === "player_roster") {
        renderPlayers(payload);
        cacheOwnCharacterCard(payload);
        rememberOwnCharacter(payload);
    } else if (type === "dm_thinking") {
        setThinking(Boolean(payload.active));
        roomState = payload.active ? "AWAITING_LLM" : "ACTIVE_TURN";
        updateTruthButton();
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
            `系统：${payload.msg}`,
            "chat-entry",
            MAX_CHAT_ENTRIES,
        );
        if (payload.msg === "游戏已开始。") {
            elements.hostModal.hidden = true;
        }
    } else if (type === "token_usage") {
        elements.tokenUsage.hidden = false;
        elements.tokenUsage.title = payload.counting_method || "估算的 token 用量";
        const used = Math.max(0, Number(payload.retained_context_tokens ?? payload.approximate_tokens) || 0);
        const limit = Math.max(1, Number(payload.context_window_size) || used || 1);
        const ratio = Math.min(1, used / limit);
        elements.tokenChart.style.background =
            `conic-gradient(var(--accent) ${ratio * 360}deg, var(--border) ${ratio * 360}deg)`;
        elements.tokenChart.setAttribute(
            "aria-label",
            `估算的保留上下文：${used.toLocaleString()} / ${limit.toLocaleString()}`,
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
        roomState = "ENDED";
        hideTruthUI();
    strictMode = false;
    activePlayerIds = [];
    elements.holdButton.hidden = true;
    elements.scheduleStatus.hidden = true;
    hasMap = false;
    mapSnapshot = null;
    elements.mapButton.hidden = true;
    elements.mapModal.hidden = true;
    hasCodex = false;
    codexSnapshot = null;
    elements.codexButton.hidden = true;
    elements.codexModal.hidden = true;
        elements.actionInput.disabled = true;
        elements.endGameButton.hidden = true;
        elements.retryRoundButton.hidden = true;
        appendText(elements.chatMessages, `系统：${payload.msg}`, "chat-entry", MAX_CHAT_ENTRIES);
    } else if (type === "room_closed") {
        roomWasClosed(payload.msg || "该房间已关闭。");
    } else if (type === "removed") {
        wasRemoved(payload.msg || "你已被移出游戏，重新加入即可继续。");
    } else if (type === "skip_vote") {
        const message = `跳过 ${payload.target}：${payload.votes}/${payload.needed}`;
        if (strictMode) {
            if (elements.scheduleStatus) {
                elements.scheduleStatus.hidden = false;
                const first = elements.scheduleStatus.firstElementChild;
                if (first) {
                    first.textContent = message;
                } else {
                    elements.scheduleStatus.textContent = message;
                }
            }
        } else {
            elements.skipVoteStatus.textContent = message;
        }
        if (payload.voter === myName) {
            votedFor = true;
            elements.skipVoteButton.disabled = true;
        }
    } else if (type === "scenario_ready") {
        roomState = "AWAITING_PLAYERS";
        updateTruthButton();
        elements.hostModal.hidden = false;
        elements.endGameButton.hidden = true;
        elements.title.textContent = payload.title;
        elements.hostStatus.textContent = `「${payload.title}」已就绪。`;
        elements.scenarioStep.hidden = true;
        elements.lobbyStep.hidden = false;
        if (payload.characters) {
            renderPlayers({ players: [], characters: payload.characters });
            rememberOwnCharacter(payload);
        }
        elements.scenarioSubmit.disabled = false;
    } else if (type === "truth_state") {
        applyTruthState(payload);
    } else if (type === "truth_question") {
        if (truthState && Array.isArray(truthState.questions)) {
            if (!truthState.questions.some((question) => question.id === payload.id)) {
                truthState.questions.push({
                    id: payload.id,
                    text: payload.text,
                    history: [],
                });
            }
            if (elements.truthModal.hidden) {
                elements.truthButton.classList.add("truth-new");
            } else {
                renderTruth();
            }
        }
    } else if (type === "truth_answer") {
        const question = truthState?.questions?.find((item) => item.id === payload.question_id);
        if (question) {
            question.history = question.history || [];
            question.history.push({ character: payload.character, text: payload.text });
            if (!elements.truthModal.hidden) {
                renderTruth();
            } else if (!ownCharacterCard || payload.character !== ownCharacterCard.name) {
                elements.truthButton.classList.add("truth-new");
            }
        }
    } else if (type === "truth_result") {
        if (payload.valid === null) {
            renderTruth();
            elements.truthError.textContent = payload.reason || "裁判暂时不可用，请稍后重试。";
        } else {
            if (truthState) {
                truthState.your_used_round = truthState.round;
            }
            renderTruth();
            if (payload.valid) {
                appendText(
                    elements.chatMessages,
                    "系统：你的可能真相已被采纳，将在下一回合供主持人参考。",
                    "chat-entry",
                    MAX_CHAT_ENTRIES,
                );
            } else {
                elements.truthError.textContent =
                    `${payload.reason || "该提交未被采纳。"}（本轮提交次数已消耗）`;
            }
        }
    } else if (type === "error") {
        showError(payload.msg || "未知的服务器错误。");
        if (!elements.keyRequiredModal.hidden) {
            elements.keyRequiredError.textContent = payload.msg || "未知的服务器错误。";
        }
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
        elements.connectionStatus.textContent = savedAuth ? "正在重新加入…" : "在线";
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
        elements.connectionStatus.textContent = "正在重连…";
        elements.actionInput.disabled = true;
        elements.chatInput.disabled = true;
        if (roomClosed) {
            elements.connectionStatus.textContent = leftRoom ? "已离开房间" : "离线";
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
        elements.connectionStatus.textContent = "连接错误";
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
            if (elements.llmModeBorrow.checked) {
                auth.llm = { mode: "borrow" };
            } else {
                const apiKey = elements.apiKey.value.trim();
                if (!apiKey) {
                    elements.loginError.textContent =
                        "请填入你自己的 API Key，或选择借用服主的 API。";
                    return;
                }
                const llm = { mode: "own", api_key: apiKey };
                const provider = elements.llmProvider.value;
                if (provider) {
                    llm.provider = provider;
                }
                const endpoint = elements.llmEndpoint.value.trim();
                if (endpoint) {
                    llm.endpoint = endpoint;
                }
                const model = elements.llmModel.value.trim();
                if (model) {
                    llm.model_name = model;
                }
                auth.llm = llm;
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
            elements.joinStatus.textContent = "正在加载角色…";
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
        elements.joinStatus.textContent = "该房间不接受新玩家。";
        return;
    }
    if (!characters.length) {
        elements.joinStatus.textContent = "该房间还未设定角色名单。";
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
    elements.llmFields.hidden = !creating;
    elements.llmOwnFields.hidden = !creating || Boolean(elements.llmModeBorrow.checked);
}

elements.llmModeOwn.addEventListener("change", () => {
    elements.llmOwnFields.hidden = false;
});
elements.llmModeBorrow.addEventListener("change", () => {
    elements.llmOwnFields.hidden = true;
});

elements.keyRequiredForm.addEventListener("submit", (event) => {
    event.preventDefault();
    elements.keyRequiredError.textContent = "";
    const apiKey = elements.keyRequiredInput.value.trim();
    if (!apiKey) {
        elements.keyRequiredError.textContent = "API Key 不能为空。";
        return;
    }
    if (send("provide_key", { api_key: apiKey })) {
        elements.keyRequiredInput.value = "";
    }
});

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
    mine.setAttribute("aria-label", "扮演该角色");
    const name = document.createElement("input");
    name.className = "character-name";
    name.maxLength = 40;
    name.placeholder = "角色名";
    const description = document.createElement("input");
    description.className = "character-description";
    description.maxLength = 50000;
    description.placeholder = "角色描述（可选）";
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
    title.placeholder = "块标题（可选）";
    title.value = block.title || "";
    const position = document.createElement("select");
    position.className = "prompt-block-position";
    ["system", "scenario", "history", "output"].forEach((value) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        position.appendChild(option);
    });
    position.value = ["system", "scenario", "history", "output"].includes(block.position)
        ? block.position
        : "output";
    const role = document.createElement("select");
    role.className = "prompt-block-role";
    ["system", "assistant", "user"].forEach((value) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        role.appendChild(option);
    });
    role.value = ["system", "assistant", "user"].includes(block.role) ? block.role : "system";
    const depth = document.createElement("input");
    depth.className = "prompt-block-depth";
    depth.type = "number";
    depth.min = "0";
    depth.max = "100";
    depth.placeholder = "depth";
    depth.title = "history 位置：距当前请求倒数的消息数";
    depth.value = Number.isFinite(block.depth) ? block.depth : 0;
    const enabledLabel = document.createElement("label");
    enabledLabel.className = "check-label prompt-block-enabled";
    const enabledCheck = document.createElement("input");
    enabledCheck.type = "checkbox";
    enabledCheck.checked = block.enabled !== false;
    enabledLabel.append(enabledCheck, "启用");
    const content = document.createElement("textarea");
    content.className = "prompt-block-content";
    content.maxLength = 10000;
    content.rows = 3;
    content.placeholder = "指令内容";
    content.value = block.content || "";
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "remove-row";
    remove.textContent = "×";
    remove.addEventListener("click", () => row.remove());
    row.append(title, position, role, depth, enabledLabel, content, remove);
    elements.promptBlocksEditor.appendChild(row);
    return row;
}

function collectPromptBlocks() {
    return [...elements.promptBlocksEditor.querySelectorAll(".prompt-block-row")]
        .map((row) => ({
            title: row.querySelector(".prompt-block-title").value.trim(),
            position: row.querySelector(".prompt-block-position").value,
            role: row.querySelector(".prompt-block-role").value,
            depth: Number.parseInt(row.querySelector(".prompt-block-depth").value, 10) || 0,
            enabled: row.querySelector(".prompt-block-enabled input").checked,
            content: row.querySelector(".prompt-block-content").value.trim(),
        }))
        .filter((block) => block.content);
}

function collectSampling() {
    const sampling = {};
    const floats = [
        ["temperature", elements.samplingTemperature],
        ["top_p", elements.samplingTopP],
        ["frequency_penalty", elements.samplingFrequencyPenalty],
        ["presence_penalty", elements.samplingPresencePenalty],
        ["min_p", elements.samplingMinP],
        ["repetition_penalty", elements.samplingRepetitionPenalty],
    ];
    for (const [name, input] of floats) {
        const value = Number.parseFloat(input.value);
        if (Number.isFinite(value)) {
            sampling[name] = value;
        }
    }
    const topK = Number.parseInt(elements.samplingTopK.value, 10);
    if (Number.isFinite(topK)) {
        sampling.top_k = topK;
    }
    return sampling;
}

let truthQuestionSerial = 0;

function updateTruthConditionFields(row) {
    const condition = row.querySelector(".truth-question-condition").value;
    row.querySelector(".truth-condition-event").hidden = condition !== "event";
    row.querySelector(".truth-condition-day").hidden = condition !== "time";
    row.querySelector(".truth-condition-clock").hidden = condition !== "time";
    row.querySelector(".truth-condition-round").hidden = condition !== "round";
}

function makeTruthQuestionRow(question = {}) {
    const row = document.createElement("div");
    row.className = "config-row truth-question-row";
    row.dataset.questionId =
        typeof question.id === "string" && question.id.trim()
            ? question.id.trim()
            : `q${++truthQuestionSerial}`;
    const text = document.createElement("input");
    text.className = "truth-question-text";
    text.maxLength = 2000;
    text.placeholder = "提问，例如：谁在说谎？";
    text.value = question.text || "";
    const condition = document.createElement("select");
    condition.className = "truth-question-condition";
    [
        ["start", "开团即出现"],
        ["event", "事件触发后"],
        ["time", "游戏内时刻"],
        ["round", "第 N 回合"],
    ].forEach(([value, label]) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = label;
        condition.appendChild(option);
    });
    condition.value = ["start", "event", "time", "round"].includes(question.condition)
        ? question.condition
        : "start";
    condition.addEventListener("change", () => updateTruthConditionFields(row));
    const eventName = document.createElement("input");
    eventName.className = "truth-condition-event";
    eventName.maxLength = 80;
    eventName.placeholder = "事件名（与定时事件一致）";
    eventName.value = question.event_name || "";
    const day = document.createElement("input");
    day.type = "number";
    day.min = 0;
    day.max = 100000;
    day.className = "truth-condition-day";
    day.title = "Day";
    day.value = question.day ?? 1;
    const clock = document.createElement("input");
    clock.type = "time";
    clock.className = "truth-condition-clock";
    clock.title = "Time";
    clock.value = minutesToTime(question.minute ?? 0);
    const round = document.createElement("input");
    round.type = "number";
    round.min = 0;
    round.max = 100000;
    round.className = "truth-condition-round";
    round.title = "第几回合";
    round.value = question.round_number ?? 1;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "remove-row";
    remove.textContent = "×";
    remove.addEventListener("click", () => row.remove());
    row.append(text, condition, eventName, day, clock, round, remove);
    elements.truthQuestionsEditor.appendChild(row);
    updateTruthConditionFields(row);
    return row;
}

function collectPlugins() {
    if (!elements.truthPluginEnabled.checked) {
        return [];
    }
    const questions = [...elements.truthQuestionsEditor.querySelectorAll(".truth-question-row")]
        .map((row) => {
            const condition = row.querySelector(".truth-question-condition").value;
            const question = {
                id: row.dataset.questionId,
                text: row.querySelector(".truth-question-text").value.trim(),
                condition,
            };
            if (condition === "event") {
                question.event_name = row.querySelector(".truth-condition-event").value.trim();
            } else if (condition === "time") {
                const [hours, minutes] = (
                    row.querySelector(".truth-condition-clock").value || "06:30"
                )
                    .split(":")
                    .map((part) => Number.parseInt(part, 10) || 0);
                question.day = Number.parseInt(row.querySelector(".truth-condition-day").value, 10) || 0;
                question.minute = hours * 60 + minutes;
            } else if (condition === "round") {
                question.round_number =
                    Number.parseInt(row.querySelector(".truth-condition-round").value, 10) || 0;
            }
            return question;
        })
        .filter((question) => question.text);
    return [
        {
            type: "possible_truths",
            enabled: true,
            config: {
                questions,
                max_answer_chars: Number.parseInt(elements.truthMaxChars.value, 10) || 500,
                require_declarative: elements.truthRequireDeclarative.checked,
                feed_dm: elements.truthFeedDm.checked,
            },
        },
    ];
}

elements.addTruthQuestion.addEventListener("click", () => makeTruthQuestionRow());
elements.truthPluginEnabled.addEventListener("change", () => {
    elements.truthPluginFields.hidden = !elements.truthPluginEnabled.checked;
});

function configInput(className, maxLength, placeholder, value = "") {
    const input = document.createElement("input");
    input.className = className;
    input.maxLength = maxLength;
    input.placeholder = placeholder;
    input.value = value;
    return input;
}

function configTextarea(className, placeholder, value = "") {
    const area = document.createElement("textarea");
    area.className = className;
    area.rows = 2;
    area.maxLength = 10000;
    area.placeholder = placeholder;
    area.value = value;
    return area;
}

function configCheck(className, label, checked) {
    const wrapper = document.createElement("label");
    wrapper.className = `check-label ${className}`;
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = checked;
    wrapper.append(input, label);
    return wrapper;
}

function configRemove(row) {
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "remove-row";
    remove.textContent = "×";
    remove.addEventListener("click", () => row.remove());
    return remove;
}

function splitList(value) {
    return value
        .split(",")
        .map((entry) => entry.trim())
        .filter(Boolean);
}

function makeRegionRow(region = {}) {
    const row = document.createElement("div");
    row.className = "config-row region-row";
    const linkText = (region.links || [])
        .map((link) => (link.requires_item ? `${link.to}>${link.requires_item}` : link.to))
        .join(", ");
    row.append(
        configInput("region-id", 64, "地区id", region.id || ""),
        configInput("region-name", 80, "名称", region.name || ""),
        configInput("region-items", 400, "物品id，逗号分隔", (region.items || []).join(", ")),
        configInput("region-links", 400, "连接：目标id 或 目标id>物品id", linkText),
        configCheck("region-private", "private", region.private === true),
        configTextarea("region-arrival", "到达固定文本", region.arrival_text || ""),
        configRemove(row),
    );
    elements.regionsEditor.appendChild(row);
    return row;
}

function makeItemRow(item = {}) {
    const row = document.createElement("div");
    row.className = "config-row item-row";
    row.append(
        configInput("item-id", 64, "物品id", item.id || ""),
        configInput("item-name", 80, "名称", item.name || ""),
        configInput("item-region", 64, "所属地区id", item.region || ""),
        configInput("item-summary", 2000, "简述", item.summary || ""),
        configCheck("item-clue", "线索", item.clue === true),
        configCheck("item-key", "关键", item.key_item === true),
        configCheck("item-carryable", "可携带", item.carryable !== false),
        configTextarea("item-investigate", "调查后的固定文本", item.investigate_text || ""),
        configRemove(row),
    );
    elements.itemsEditor.appendChild(row);
    return row;
}

function collectRegions() {
    return [...elements.regionsEditor.querySelectorAll(".region-row")]
        .map((row) => ({
            id: row.querySelector(".region-id").value.trim(),
            name: row.querySelector(".region-name").value.trim(),
            arrival_text: row.querySelector(".region-arrival").value.trim(),
            items: splitList(row.querySelector(".region-items").value),
            links: splitList(row.querySelector(".region-links").value).map((entry) => {
                const [to, requiresItem] = entry.split(">");
                const link = { to: to.trim() };
                if (requiresItem && requiresItem.trim()) {
                    link.requires_item = requiresItem.trim();
                }
                return link;
            }),
            private: row.querySelector(".region-private input").checked,
        }))
        .filter((region) => region.id && region.name);
}

function collectItems() {
    return [...elements.itemsEditor.querySelectorAll(".item-row")]
        .map((row) => ({
            id: row.querySelector(".item-id").value.trim(),
            name: row.querySelector(".item-name").value.trim(),
            region: row.querySelector(".item-region").value.trim(),
            summary: row.querySelector(".item-summary").value.trim(),
            investigate_text: row.querySelector(".item-investigate").value.trim(),
            clue: row.querySelector(".item-clue input").checked,
            key_item: row.querySelector(".item-key input").checked,
            carryable: row.querySelector(".item-carryable input").checked,
        }))
        .filter((item) => item.id && item.name);
}

function collectWorld() {
    const regions = collectRegions();
    const items = collectItems();
    const world = regions.length
        ? { start_region: elements.worldStartRegion.value.trim() || regions[0].id }
        : null;
    return { world, regions, items };
}

elements.addRegion.addEventListener("click", () => makeRegionRow());
elements.addItem.addEventListener("click", () => makeItemRow());

elements.addPromptBlock.addEventListener("click", () => makePromptBlockRow());

function makeTimeRuleRow(activity = "", minimum = "", maximum = "", keys = []) {
    const row = document.createElement("div");
    row.className = "config-row time-rule-row";
    const activityInput = document.createElement("input");
    activityInput.className = "rule-activity";
    activityInput.maxLength = 80;
    activityInput.placeholder = "活动（例如 搜查房间）";
    activityInput.value = activity;
    const keysInput = document.createElement("input");
    keysInput.className = "rule-keys";
    keysInput.maxLength = 400;
    keysInput.placeholder = "关键词，逗号分隔（可选）";
    keysInput.value = (Array.isArray(keys) ? keys : []).join(", ");
    const minInput = document.createElement("input");
    minInput.type = "number";
    minInput.min = 0;
    minInput.max = 525600;
    minInput.className = "rule-min";
    minInput.placeholder = "最少";
    minInput.value = minimum;
    const maxInput = document.createElement("input");
    maxInput.type = "number";
    maxInput.min = 0;
    maxInput.max = 525600;
    maxInput.className = "rule-max";
    maxInput.placeholder = "最多";
    maxInput.value = maximum;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "remove-row";
    remove.textContent = "×";
    remove.addEventListener("click", () => row.remove());
    row.append(activityInput, keysInput, minInput, maxInput, remove);
    elements.timeRulesEditor.appendChild(row);
    return row;
}

function makeEventRow(event = {}) {
    const row = document.createElement("div");
    row.className = "config-row event-row";
    const name = document.createElement("input");
    name.className = "event-name";
    name.maxLength = 80;
    name.placeholder = "事件名";
    name.value = event.name || "";
    const region = document.createElement("input");
    region.className = "event-region";
    region.maxLength = 64;
    region.placeholder = "地区id（填了就按地区触发）";
    region.value = event.region || "";
    const when = document.createElement("select");
    when.className = "event-when";
    [
        ["enter", "进入地区"],
        ["time", "该地区到点"],
    ].forEach(([value, label]) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = label;
        when.appendChild(option);
    });
    when.value = event.when === "time" ? "time" : "enter";
    const actor = document.createElement("input");
    actor.className = "event-actor";
    actor.maxLength = 80;
    actor.placeholder = "NPC名（可选）";
    actor.value = event.actor || "";
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
    description.placeholder = "将发生什么（后台）";
    description.value = event.description || "";
    const publicLabel = document.createElement("label");
    publicLabel.className = "check-label event-public";
    const publicCheck = document.createElement("input");
    publicCheck.type = "checkbox";
    publicCheck.checked = Boolean(event.public);
    publicCheck.title = "向全体玩家公布";
    publicLabel.append(publicCheck, "public");
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "remove-row";
    remove.textContent = "×";
    remove.addEventListener("click", () => row.remove());
    row.append(name, region, when, actor, day, time, description, publicLabel, remove);
    elements.eventsEditor.appendChild(row);
    return row;
}

function makeLorebookRow(entry = {}) {
    const row = document.createElement("div");
    row.className = "config-row lorebook-row";
    const title = document.createElement("input");
    title.className = "lorebook-title";
    title.maxLength = 80;
    title.placeholder = "标题（可选）";
    title.value = entry.title || "";
    const keys = document.createElement("input");
    keys.className = "lorebook-keys";
    keys.maxLength = 1000;
    keys.placeholder = "关键词，逗号分隔";
    keys.value = (entry.keys || []).join(", ");
    const content = document.createElement("textarea");
    content.className = "lorebook-content";
    content.maxLength = 10000;
    content.rows = 2;
    content.placeholder = "关键词出现时注入的内容";
    content.value = entry.content || "";
    const order = document.createElement("input");
    order.type = "number";
    order.min = 0;
    order.max = 10000;
    order.className = "lorebook-order";
    order.title = "插入顺序（越高影响越强）";
    order.value = entry.order ?? 0;
    const role = document.createElement("select");
    role.className = "lorebook-role";
    ["system", "assistant", "user"].forEach((value) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        role.appendChild(option);
    });
    role.value = ["system", "assistant", "user"].includes(entry.role) ? entry.role : "system";
    role.title = "in-chat 注入角色（仅当填写深度时生效）";
    const depth = document.createElement("input");
    depth.type = "number";
    depth.min = 0;
    depth.max = 100;
    depth.className = "lorebook-depth";
    depth.placeholder = "深度";
    depth.title = "留空=并入世界书文本；填 0-100=按 depth 作为消息注入";
    depth.value = entry.depth === null || entry.depth === undefined ? "" : entry.depth;
    const constantLabel = document.createElement("label");
    constantLabel.className = "check-label lorebook-constant";
    const constantCheck = document.createElement("input");
    constantCheck.type = "checkbox";
    constantCheck.checked = Boolean(entry.constant);
    constantCheck.title = "常驻";
    constantLabel.append(constantCheck, "always");
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "remove-row";
    remove.textContent = "×";
    remove.addEventListener("click", () => row.remove());
    row.append(title, keys, content, order, role, depth, constantLabel, remove);
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
            keys: row
                .querySelector(".rule-keys")
                .value.split(",")
                .map((key) => key.trim())
                .filter(Boolean),
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
    return [...elements.eventsEditor.querySelectorAll(".event-row")]
        .map((row) => {
            const [hours, minutes] = (row.querySelector(".event-time").value || "06:30")
                .split(":")
                .map((part) => parseInt(part, 10) || 0);
            const region = row.querySelector(".event-region").value.trim();
            return {
                name: row.querySelector(".event-name").value.trim(),
                day: parseInt(row.querySelector(".event-day").value, 10) || 0,
                minute: hours * 60 + minutes,
                description: row.querySelector(".event-description").value.trim(),
                public: row.querySelector(".event-public input").checked,
                region,
                when: region ? row.querySelector(".event-when").value : "",
                actor: row.querySelector(".event-actor").value.trim(),
            };
        })
        .filter((event) => event.name && event.description);
}

function collectLorebook() {
    return [...elements.lorebookEditor.querySelectorAll(".lorebook-row")]
        .map((row) => {
            const rawDepth = row.querySelector(".lorebook-depth").value.trim();
            const depth = rawDepth === "" ? null : Number.parseInt(rawDepth, 10);
            return {
                title: row.querySelector(".lorebook-title").value.trim(),
                keys: row
                    .querySelector(".lorebook-keys")
                    .value.split(",")
                    .map((key) => key.trim())
                    .filter(Boolean),
                content: row.querySelector(".lorebook-content").value.trim(),
                order: parseInt(row.querySelector(".lorebook-order").value, 10) || 0,
                role: row.querySelector(".lorebook-role").value,
                depth: Number.isFinite(depth) ? depth : null,
                constant: row.querySelector(".lorebook-constant input").checked,
            };
        })
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
    if (typeof config.opening === "string") {
        elements.opening.value = config.opening;
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
            makeTimeRuleRow(
                rule.activity,
                rule.minutes_min ?? "",
                rule.minutes_max ?? "",
                Array.isArray(rule.keys) ? rule.keys : [],
            );
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
                region: typeof event.region === "string" ? event.region : "",
                when: event.when === "time" ? "time" : event.region ? "enter" : "",
                actor: typeof event.actor === "string" ? event.actor : "",
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
        if (Number.isFinite(config.sampling.frequency_penalty)) {
            elements.samplingFrequencyPenalty.value = config.sampling.frequency_penalty;
        }
        if (Number.isFinite(config.sampling.presence_penalty)) {
            elements.samplingPresencePenalty.value = config.sampling.presence_penalty;
        }
        if (Number.isFinite(config.sampling.top_k)) {
            elements.samplingTopK.value = config.sampling.top_k;
        }
        if (Number.isFinite(config.sampling.min_p)) {
            elements.samplingMinP.value = config.sampling.min_p;
        }
        if (Number.isFinite(config.sampling.repetition_penalty)) {
            elements.samplingRepetitionPenalty.value = config.sampling.repetition_penalty;
        }
    }
    const truthPlugin = (Array.isArray(config.plugins) ? config.plugins : []).find(
        (plugin) => plugin && plugin.type === "possible_truths",
    );
    if (truthPlugin) {
        const pluginConfig =
            truthPlugin.config && typeof truthPlugin.config === "object" ? truthPlugin.config : {};
        const enabled = truthPlugin.enabled !== false;
        elements.truthPluginEnabled.checked = enabled;
        elements.truthPluginFields.hidden = !enabled;
        if (Number.isFinite(pluginConfig.max_answer_chars)) {
            elements.truthMaxChars.value = pluginConfig.max_answer_chars;
        }
        if (typeof pluginConfig.require_declarative === "boolean") {
            elements.truthRequireDeclarative.checked = pluginConfig.require_declarative;
        }
        if (typeof pluginConfig.feed_dm === "boolean") {
            elements.truthFeedDm.checked = pluginConfig.feed_dm;
        }
        (Array.isArray(pluginConfig.questions) ? pluginConfig.questions : []).forEach(
            (question) => {
                if (question && typeof question.text === "string" && question.text.trim()) {
                    makeTruthQuestionRow(question);
                }
            },
        );
    }
    if (typeof config.random_turn_order === "boolean") {
        elements.randomTurnOrder.checked = config.random_turn_order;
    }
    if (typeof config.schedule === "string") {
        elements.scheduleStrict.checked = config.schedule === "strict";
    }
    if (config.world && typeof config.world === "object" && !Array.isArray(config.world)) {
        if (typeof config.world.start_region === "string") {
            elements.worldStartRegion.value = config.world.start_region;
        }
    }
    (Array.isArray(config.regions) ? config.regions : []).forEach((region) => {
        if (region && typeof region.id === "string" && region.name) {
            makeRegionRow(region);
        }
    });
    (Array.isArray(config.items) ? config.items : []).forEach((item) => {
        if (item && typeof item.id === "string" && item.name) {
            makeItemRow(item);
        }
    });
}

function exportScenarioConfig() {
    const { characters, hostCharacter } = collectCharacters();
    const config = {
        scenario: elements.scenario.value,
        opening: elements.opening.value,
        guidance: elements.guidance.value,
        characters,
        host_character: hostCharacter || "",
        time_config: collectTimeConfig(),
        time_rules: collectTimeRules(),
        events: collectEvents(),
        lorebook: collectLorebook(),
        prompt_blocks: collectPromptBlocks(),
        sampling: collectSampling(),
        plugins: collectPlugins(),
        random_turn_order: elements.randomTurnOrder.checked,
        schedule: elements.scheduleStrict.checked ? "strict" : "round",
        ...collectWorld(),
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
            elements.hostStatus.textContent = "配置已导入。";
        } catch (error) {
            elements.hostStatus.textContent = `Could not load config: ${error.message}`;
        }
    };
    reader.onerror = () => {
        elements.hostStatus.textContent = "无法读取配置文件。";
    };
    reader.readAsText(file);
    elements.loadConfigInput.value = "";
});

elements.exportConfigButton.addEventListener("click", () => {
    exportScenarioConfig();
    elements.hostStatus.textContent = "配置已导出。";
});

elements.importStButton.addEventListener("click", () => {
    elements.importStInput.click();
});

elements.importStInput.addEventListener("change", async () => {
    const file = elements.importStInput.files && elements.importStInput.files[0];
    if (!file) {
        return;
    }
    elements.hostStatus.textContent = "正在导入 ST 文件…";
    try {
        const response = await fetch("/st/import", { method: "POST", body: file });
        const payload = await response.json();
        if (!response.ok) {
            throw new Error(payload.error || `HTTP ${response.status}`);
        }
        applyScenarioConfig(payload.config || {});
        const warnings = payload.warnings || [];
        if (warnings.length) {
            elements.importWarnings.hidden = false;
            elements.importWarnings.textContent =
                "导入完成；以下内容未映射：" + warnings.join(" ");
        } else {
            elements.importWarnings.hidden = true;
            elements.importWarnings.textContent = "";
        }
        elements.hostStatus.textContent = "ST 配置已导入。";
    } catch (error) {
        elements.hostStatus.textContent = `导入失败：${error.message}`;
    } finally {
        elements.importStInput.value = "";
    }
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
        elements.hostStatus.textContent = "每个角色都需要名字。";
        return;
    }
    const timeConfig = collectTimeConfig();
    const events = collectEvents();
    if (events.some((event) => !event.region) && !timeConfig.enabled) {
        elements.hostStatus.textContent = "定时事件需要启用游戏内时钟。";
        return;
    }
    if (
        send("scenario_init", {
            scenario: elements.scenario.value.trim(),
            opening: elements.opening.value.trim(),
            guidance: elements.guidance.value.trim(),
            characters,
            host_character: hostCharacter || "",
            time_config: timeConfig,
            time_rules: collectTimeRules(),
            events,
            lorebook: collectLorebook(),
            prompt_blocks: collectPromptBlocks(),
            sampling: collectSampling(),
            plugins: collectPlugins(),
            random_turn_order: elements.randomTurnOrder.checked,
            schedule: elements.scheduleStrict.checked ? "strict" : "round",
            ...collectWorld(),
        })
    ) {
        elements.scenarioSubmit.disabled = true;
        elements.hostStatus.textContent = "正在生成剧情…";
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
    if (window.confirm("为所有玩家结束本局游戏？")) {
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
        elements.hostStatus.textContent = "正在开始游戏…";
    }
});

elements.closeRoomButton.addEventListener("click", () => {
    if (window.confirm("为所有玩家关闭该房间？")) {
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
        elements.holdButton.hidden = true;
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

elements.holdButton.addEventListener("click", holdTurn);

makeCharacterRow();
makeCharacterRow();
