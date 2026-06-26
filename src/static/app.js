/* ==============================================================================
   PROJECT JANUS: CLIENT-SIDE APP LOGIC
   ============================================================================== */

// Intercept native fetch to inject JWT authorization and handle 401s
const originalFetch = window.fetch;
window.fetch = async function (url, options = {}) {
    const token = localStorage.getItem("janus_token");
    if (token) {
        options.headers = options.headers || {};
        if (!(options.headers instanceof Headers)) {
            options.headers["Authorization"] = `Bearer ${token}`;
        } else {
            options.headers.set("Authorization", `Bearer ${token}`);
        }
    }
    
    const response = await originalFetch(url, options);
    
    // Automatically boot to login screen on 401 Unauthorized
    if (response.status === 401 && !url.includes("/api/v1/auth/token")) {
        localStorage.removeItem("janus_token");
        window.location.reload();
    }
    
    return response;
};

document.addEventListener("DOMContentLoaded", () => {
    // DOM Elements
    const chatMessages = document.getElementById("chat-messages-container");
    const chatForm = document.getElementById("chat-form");
    const chatInput = document.getElementById("chat-input");
    const deliberationsTimeline = document.getElementById("deliberations-timeline-container");
    
    // Modal Elements
    const auditModal = document.getElementById("audit-modal");
    const modalActionName = document.getElementById("modal-action-name");
    const modalCriticVerdict = document.getElementById("modal-critic-verdict");
    const modalUtilityScore = document.getElementById("modal-utility-score");
    const modalCriticJustification = document.getElementById("modal-critic-justification");
    const modalRawJson = document.getElementById("modal-raw-json");
    const closeModalBtn = document.getElementById("close-modal-btn");

    // Local state cache to avoid redraw flashes on timeline polling
    let cachedDelibIds = new Set();

    // Initialize App
    checkAuthAndInit();

    function checkAuthAndInit() {
        const token = localStorage.getItem("janus_token");
        const appWrapper = document.getElementById("app-wrapper");
        const loginScreen = document.getElementById("login-screen");
        
        if (token) {
            loginScreen.style.display = "none";
            appWrapper.style.display = "flex";
            init();
        } else {
            appWrapper.style.display = "none";
            loginScreen.style.display = "flex";
            setupLoginListeners();
        }
    }

    function setupLoginListeners() {
        const loginForm = document.getElementById("login-form");
        const loginUsernameInput = document.getElementById("login-username");
        const loginKeyInput = document.getElementById("login-key");
        const loginErrorMsg = document.getElementById("login-error-msg");
        
        loginForm.addEventListener("submit", async (e) => {
            e.preventDefault();
            const usernameOrId = loginUsernameInput.value.trim();
            const enrollmentKey = loginKeyInput.value.trim();
            
            if (!usernameOrId || !enrollmentKey) return;
            
            loginErrorMsg.style.display = "none";
            
            try {
                const res = await originalFetch("/api/v1/auth/token", {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json"
                    },
                    body: JSON.stringify({
                        username_or_id: usernameOrId,
                        enrollment_key: enrollmentKey
                    })
                });
                
                if (res.status === 200) {
                    const data = await res.json();
                    localStorage.setItem("janus_token", data.access_token);
                    checkAuthAndInit();
                } else {
                    const data = await res.json().catch(() => ({}));
                    loginErrorMsg.textContent = data.detail || "Authentication failed. Invalid username, ID, or key.";
                    loginErrorMsg.style.display = "block";
                }
            } catch (err) {
                console.error("Login request failed:", err);
                loginErrorMsg.textContent = "Unable to connect to security service.";
                loginErrorMsg.style.display = "block";
            }
        });
    }

    function init() {
        // Logout control
        document.getElementById("btn-logout").addEventListener("click", () => {
            localStorage.removeItem("janus_token");
            window.location.reload();
        });

        // Init Tab Switching
        initTabs();

        // Load initial chat history
        loadChatHistory();

        // Load initial deliberations and start polling
        loadDeliberations();
        setInterval(loadDeliberations, 4000); // Poll deliberations every 4s

        // Poll Sandbox and Staging statuses every 4 seconds to sync widgets
        setInterval(loadSandboxStatus, 4000);
        setInterval(loadStagingStatus, 4000);

        // Event Listeners
        chatForm.addEventListener("submit", handleChatSubmit);
        
        chatInput.addEventListener("keydown", (e) => {
            if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                chatForm.requestSubmit();
            }
        });

        chatInput.addEventListener("input", () => {
            chatInput.style.height = "auto";
            chatInput.style.height = chatInput.scrollHeight + "px";
        });
        closeModalBtn.addEventListener("click", () => closeModal());
        
        // Sandbox controls
        document.getElementById("btn-sandbox-start").addEventListener("click", handleSandboxStart);
        document.getElementById("btn-sandbox-test").addEventListener("click", handleSandboxTest);
        document.getElementById("btn-sandbox-ship").addEventListener("click", handleSandboxShip);
        document.getElementById("btn-sandbox-abort").addEventListener("click", handleSandboxAbort);

        // Staging controls
        document.getElementById("btn-stage-apply").addEventListener("click", handleStageApply);
        document.getElementById("btn-stage-heal").addEventListener("click", handleStageHeal);
        document.getElementById("btn-stage-cancel").addEventListener("click", handleStageCancel);
        document.getElementById("btn-stage-refine").addEventListener("click", handleStageRefine);

        // Config controls
        document.getElementById("btn-constitution-add").addEventListener("click", handleConstitutionAdd);
        document.getElementById("btn-rule-add").addEventListener("click", handleAgentRuleAdd);

        // Close modal when clicking overlay
        auditModal.addEventListener("click", (e) => {
            if (e.target === auditModal) {
                closeModal();
            }
        });

        // ESC key closes modal
        document.addEventListener("keydown", (e) => {
            if (e.key === "Escape" && auditModal.classList.contains("active")) {
                closeModal();
            }
        });
    }

    // --- Navigation Tabs ---
    function initTabs() {
        const navBtns = document.querySelectorAll(".nav-btn");
        const tabContents = document.querySelectorAll(".tab-content");

        navBtns.forEach(btn => {
            btn.addEventListener("click", () => {
                const targetTab = btn.getAttribute("data-tab");
                
                // Toggle nav button active state
                navBtns.forEach(b => b.classList.remove("active"));
                btn.classList.add("active");

                // Toggle tab content visibility
                tabContents.forEach(tab => {
                    if (tab.id === targetTab) {
                        tab.style.display = targetTab === "chat-tab" ? "grid" : "flex";
                        tab.classList.add("active");
                    } else {
                        tab.style.display = "none";
                        tab.classList.remove("active");
                    }
                });

                // Load config views when config tab is selected
                if (targetTab === "config-tab") {
                    loadConstitution();
                    loadAgentRegistry();
                    loadAgentRules();
                } else if (targetTab === "sandbox-tab") {
                    loadSandboxStatus();
                } else if (targetTab === "staging-tab") {
                    loadStagingStatus();
                }
            });
        });
    }

    // --- Chat Functions ---

    async function loadChatHistory() {
        try {
            const res = await fetch("/api/history");
            const history = await res.json();
            
            if (history && history.length > 0) {
                chatMessages.innerHTML = "";
                history.forEach(msg => {
                    appendMessage(msg.speaker, msg.message);
                });
                scrollToBottom();
            }
        } catch (err) {
            console.error("Failed to load chat history:", err);
        }
    }

    async function handleChatSubmit(e) {
        e.preventDefault();
        const text = chatInput.value.trim();
        if (!text) return;

        // Clear input field immediately
        chatInput.value = "";
        chatInput.style.height = "auto";

        // Append user message
        appendMessage("user", text);
        scrollToBottom();

        // Append typing indicator
        const indicator = appendTypingIndicator();
        scrollToBottom();

        try {
            const res = await fetch("/api/chat", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify({ message: text })
            });

            const rawText = await res.text();
            indicator.remove();

            let data;
            try {
                data = JSON.parse(rawText);
            } catch (_) {
                appendMessage("persona", `*(Swarm core returned an unreadable response — status ${res.status})*`);
                scrollToBottom();
                return;
            }

            if (!res.ok) {
                const detail = data.detail || data.error || `HTTP ${res.status}`;
                appendMessage("persona", `*(System Error: ${detail})*`);
            } else if (data.response) {
                appendMessage("persona", data.response);
            } else if (data.error) {
                appendMessage("persona", `*(System Error: ${data.error})*`);
            }
            scrollToBottom();
        } catch (err) {
            indicator.remove();
            appendMessage("persona", `*(Failed to reach swarm core: ${err.message})*`);
            scrollToBottom();
        }
    }

    function appendMessage(speaker, text) {
        const msgDiv = document.createElement("div");
        msgDiv.classList.add("message", speaker);
        
        // Simple markdown parsing helper
        msgDiv.innerHTML = formatMessageText(text);
        chatMessages.appendChild(msgDiv);
    }

    function appendTypingIndicator() {
        const indicator = document.createElement("div");
        indicator.className = "typing-indicator message persona";
        indicator.innerHTML = `
            <div class="typing-dot"></div>
            <div class="typing-dot"></div>
            <div class="typing-dot"></div>
        `;
        chatMessages.appendChild(indicator);
        return indicator;
    }

    function scrollToBottom() {
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }

    function formatMessageText(text) {
        let escaped = text
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;");

        escaped = escaped.replace(/`([^`]+)`/g, "<code>$1</code>");

        const lines = escaped.split("\n");
        let inList = false;
        const formattedLines = [];

        lines.forEach(line => {
            const trimmed = line.trim();
            if (trimmed.startsWith("- ") || trimmed.startsWith("* ")) {
                if (!inList) {
                    formattedLines.push("<ul>");
                    inList = true;
                }
                formattedLines.push(`<li>${trimmed.slice(2)}</li>`);
            } else {
                if (inList) {
                    formattedLines.push("</ul>");
                    inList = false;
                }
                formattedLines.push(`<p>${line}</p>`);
            }
        });
        if (inList) {
            formattedLines.push("</ul>");
        }

        return formattedLines.join("");
    }

    // --- Deliberations Timeline Functions ---

    async function loadDeliberations() {
        try {
            const res = await fetch("/api/deliberations");
            const deliberations = await res.json();

            if (!deliberations || deliberations.length === 0) {
                return;
            }

            const currentIds = new Set(deliberations.map(d => d.id));
            const hasNew = [...currentIds].some(id => !cachedDelibIds.has(id));
            
            if (hasNew || deliberations.length !== cachedDelibIds.size) {
                cachedDelibIds = currentIds;
                renderDeliberations(deliberations);
            }
        } catch (err) {
            console.error("Failed to load deliberations:", err);
        }
    }

    function renderDeliberations(delibs) {
        deliberationsTimeline.innerHTML = "";
        
        delibs.forEach(d => {
            const card = document.createElement("article");
            const decisionClass = d.decision === 1 ? "approved" : "vetoed";
            const decisionLabel = d.decision === 1 ? "Approved" : "Vetoed";

            card.className = `delib-card ${decisionClass}`;
            
            let timeStr = d.timestamp;
            try {
                if (timeStr.includes(" ")) {
                    timeStr = timeStr.split(" ")[1];
                }
            } catch (_) {}

            card.innerHTML = `
                <div class="delib-header-row">
                    <span class="delib-time">${timeStr}</span>
                    <span class="badge ${decisionClass}">${decisionLabel}</span>
                </div>
                <h3 class="delib-action">${d.action}</h3>
                <p class="delib-justification">${d.justification}</p>
            `;

            card.addEventListener("click", () => openModal(d));

            deliberationsTimeline.appendChild(card);
        });
    }

    // --- Git Sandbox Functions ---
    async function loadSandboxStatus() {
        try {
            const res = await fetch("/api/sandbox/status");
            const data = await res.json();
            
            const inactiveArea = document.getElementById("sandbox-inactive-controls");
            const activeArea = document.getElementById("sandbox-active-controls");
            const detailsArea = document.getElementById("sandbox-details-area");
            const diffArea = document.getElementById("sandbox-diff-area");
            const emptyState = document.getElementById("sandbox-empty-state");
            
            if (data.active) {
                inactiveArea.style.display = "none";
                activeArea.style.display = "flex";
                detailsArea.style.display = "grid";
                emptyState.style.display = "none";
                
                document.getElementById("sandbox-status-badge").textContent = data.status;
                document.getElementById("sandbox-val-path").textContent = data.path;
                document.getElementById("sandbox-val-branch").textContent = data.branch;
                
                const filesList = document.getElementById("sandbox-val-files");
                filesList.innerHTML = "";
                if (data.modified && data.modified.length > 0) {
                    data.modified.forEach(f => {
                        const li = document.createElement("li");
                        li.textContent = f;
                        filesList.appendChild(li);
                    });
                } else {
                    const li = document.createElement("li");
                    li.textContent = "None";
                    filesList.appendChild(li);
                }
                
                document.getElementById("sandbox-val-logs").textContent = data.test_logs || "Waiting for test execution...";
                
                loadSandboxDiff();
            } else {
                inactiveArea.style.display = "flex";
                activeArea.style.display = "none";
                detailsArea.style.display = "none";
                diffArea.style.display = "none";
                emptyState.style.display = "block";
            }
        } catch (err) {
            console.error("Error loading sandbox status:", err);
        }
    }

    async function loadSandboxDiff() {
        try {
            const res = await fetch("/api/sandbox/diff");
            const data = await res.json();
            const diffArea = document.getElementById("sandbox-diff-area");
            const diffCode = document.getElementById("sandbox-diff-code");
            
            if (data.diff && data.diff.trim()) {
                diffArea.style.display = "block";
                diffCode.textContent = data.diff;
            } else {
                diffArea.style.display = "none";
            }
        } catch (err) {
            console.error("Error loading sandbox diff:", err);
        }
    }

    async function handleSandboxStart() {
        const nameInput = document.getElementById("sandbox-name-input");
        const name = nameInput.value.trim();
        if (!name) return;
        
        nameInput.value = "";
        try {
            const res = await fetch("/api/sandbox/action", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ action: "start", name: name })
            });
            const data = await res.json();
            if (data.success) {
                loadSandboxStatus();
            } else {
                alert(`Error: ${data.error || "Failed to start sandbox"}`);
            }
        } catch (err) {
            console.error(err);
        }
    }

    async function handleSandboxTest() {
        const logsBox = document.getElementById("sandbox-val-logs");
        logsBox.textContent = "Executing Pytests inside Git Sandbox Worktree...\n(This might take several seconds)";
        try {
            const res = await fetch("/api/sandbox/action", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ action: "test" })
            });
            const data = await res.json();
            if (data.success) {
                loadSandboxStatus();
            } else {
                alert(`Error: ${data.error}`);
            }
        } catch (err) {
            console.error(err);
        }
    }

    async function handleSandboxShip() {
        if (!confirm("Are you sure you want to merge and ship all sandbox changes to the active workspace?")) return;
        try {
            const res = await fetch("/api/sandbox/action", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ action: "ship" })
            });
            const data = await res.json();
            if (data.success) {
                loadSandboxStatus();
                appendMessage("system", `Sandbox shipped successfully. Modified files: ${data.copied.join(", ")}`);
            } else {
                alert(`Error: ${data.error}`);
            }
        } catch (err) {
            console.error(err);
        }
    }

    async function handleSandboxAbort() {
        if (!confirm("Are you sure you want to abort this sandbox session? All changes will be lost permanently.")) return;
        try {
            const res = await fetch("/api/sandbox/action", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ action: "abort" })
            });
            const data = await res.json();
            if (data.success) {
                loadSandboxStatus();
                appendMessage("system", "Sandbox session aborted and deleted.");
            } else {
                alert(`Error: ${data.error}`);
            }
        } catch (err) {
            console.error(err);
        }
    }

    // --- Staging Area Functions ---
    async function loadStagingStatus() {
        try {
            const res = await fetch("/api/stage/status");
            const data = await res.json();
            
            const activeControls = document.getElementById("staging-active-controls");
            const detailsArea = document.getElementById("staging-details-area");
            const diffArea = document.getElementById("staging-diff-area");
            const emptyState = document.getElementById("staging-empty-state");
            
            if (data.active) {
                activeControls.style.display = "flex";
                detailsArea.style.display = "grid";
                emptyState.style.display = "none";
                
                document.getElementById("staging-status-badge").textContent = `STAGED (${data.status.toUpperCase()})`;
                document.getElementById("stage-val-files").textContent = data.file_path;
                document.getElementById("stage-val-path").textContent = data.dir;
                document.getElementById("stage-val-logs").textContent = data.test_logs || "No test run failure logs.";
                
                if (data.diff) {
                    diffArea.style.display = "block";
                    document.getElementById("staging-diff-code").textContent = data.diff;
                } else {
                    diffArea.style.display = "none";
                }
                
                const select = document.getElementById("stage-refine-file-select");
                select.innerHTML = "";
                const files = data.file_path.split(",");
                files.forEach(f => {
                    const option = document.createElement("option");
                    option.value = f;
                    option.textContent = f;
                    select.appendChild(option);
                });
            } else {
                activeControls.style.display = "none";
                detailsArea.style.display = "none";
                diffArea.style.display = "none";
                emptyState.style.display = "block";
            }
        } catch (err) {
            console.error("Error loading staging status:", err);
        }
    }

    async function handleStageApply() {
        try {
            const res = await fetch("/api/stage/action", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ action: "apply" })
            });
            const data = await res.json();
            if (data.success) {
                loadStagingStatus();
                appendMessage("system", "Staged changes successfully applied to active codebase.");
            } else {
                alert(`Error: ${data.error}`);
            }
        } catch (err) {
            console.error(err);
        }
    }

    async function handleStageCancel() {
        if (!confirm("Are you sure you want to discard these staged modifications?")) return;
        try {
            const res = await fetch("/api/stage/action", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ action: "cancel" })
            });
            const data = await res.json();
            if (data.success) {
                loadStagingStatus();
                appendMessage("system", "Staging workspace cleaned and discarded.");
            } else {
                alert(`Error: ${data.error}`);
            }
        } catch (err) {
            console.error(err);
        }
    }

    async function handleStageHeal() {
        const logsBox = document.getElementById("stage-val-logs");
        logsBox.textContent = "The Membrane is self-healing pre-existing test failures asynchronously...\n(This might take several seconds)";
        try {
            const res = await fetch("/api/stage/action", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ action: "heal" })
            });
            const data = await res.json();
            if (data.success) {
                loadStagingStatus();
                alert(data.passed ? "Self-healing PASSED all tests!" : "Self-healing completed, but tests still failing. Check logs.");
            } else {
                alert(`Error: ${data.error}`);
            }
        } catch (err) {
            console.error(err);
        }
    }

    async function handleStageRefine() {
        const fileSelect = document.getElementById("stage-refine-file-select");
        const file_path = fileSelect.value;
        const instInput = document.getElementById("stage-refine-instructions");
        const instructions = instInput.value.trim();
        if (!file_path || !instructions) return;
        
        instInput.value = "";
        const logsBox = document.getElementById("stage-val-logs");
        logsBox.textContent = `Regenerating modifications for '${file_path}' asynchronously...\n(This might take several seconds)`;
        
        try {
            const res = await fetch("/api/stage/action", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ action: "refine", file_path: file_path, instructions: instructions })
            });
            const data = await res.json();
            if (data.success) {
                loadStagingStatus();
            } else {
                alert(`Error: ${data.error}`);
            }
        } catch (err) {
            console.error(err);
        }
    }

    // --- Configuration Functions ---
    async function loadConstitution() {
        try {
            const res = await fetch("/api/constitution");
            const data = await res.json();
            const tbody = document.querySelector("#constitution-table tbody");
            tbody.innerHTML = "";
            
            data.forEach(r => {
                const tr = document.createElement("tr");
                tr.innerHTML = `
                    <td><strong>${r.key}</strong></td>
                    <td>${r.text}</td>
                    <td>
                        <button class="btn btn-secondary btn-delete-constitution-rule" data-key="${r.key}">&times;</button>
                    </td>
                `;
                tbody.appendChild(tr);
            });

            // Bind delete rule event
            document.querySelectorAll(".btn-delete-constitution-rule").forEach(btn => {
                btn.addEventListener("click", () => {
                    const ruleKey = btn.getAttribute("data-key");
                    if (confirm(`Are you sure you want to repeal constitution rule '${ruleKey}'?`)) {
                        handleConstitutionDelete(ruleKey);
                    }
                });
            });
        } catch (err) {
            console.error("Error loading constitution:", err);
        }
    }

    async function handleConstitutionDelete(ruleKey) {
        try {
            const res = await fetch("/api/constitution/delete", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ key: ruleKey })
            });
            const data = await res.json();
            if (data.success) {
                loadConstitution();
                appendMessage("system", `Repealed constitution rule '${ruleKey}'`);
            } else {
                alert(`Error: ${data.error}`);
            }
        } catch (err) {
            console.error(err);
        }
    }

    async function handleConstitutionAdd() {
        const keyInput = document.getElementById("constitution-key-input");
        const textInput = document.getElementById("constitution-text-input");
        const key = keyInput.value.trim().toUpperCase();
        const text = textInput.value.trim();
        
        if (!key || !text) return;
        keyInput.value = "";
        textInput.value = "";
        
        try {
            const res = await fetch("/api/constitution/amend", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ key: key, text: text })
            });
            const data = await res.json();
            if (data.success) {
                loadConstitution();
                appendMessage("system", `Amended constitution: Sealed rule '${key}'`);
            } else {
                alert(`Error: ${data.error}`);
            }
        } catch (err) {
            console.error(err);
        }
    }

    async function loadAgentRegistry() {
        try {
            const res = await fetch("/api/registry");
            const data = await res.json();
            const tbody = document.querySelector("#registry-table tbody");
            tbody.innerHTML = "";
            
            data.forEach(agent => {
                const tr = document.createElement("tr");
                tr.innerHTML = `
                    <td><strong>${agent.name}</strong></td>
                    <td><code>${agent.id}</code></td>
                    <td>
                        <input type="text" class="agent-model-input" data-agent="${agent.id}" value="${agent.model}" placeholder="Default global model">
                    </td>
                    <td>
                        <button class="btn btn-primary btn-save-model" data-agent="${agent.id}">Save</button>
                    </td>
                `;
                tbody.appendChild(tr);
            });
            
            document.querySelectorAll(".btn-save-model").forEach(btn => {
                btn.addEventListener("click", () => {
                    const agentId = btn.getAttribute("data-agent");
                    const input = document.querySelector(`.agent-model-input[data-agent="${agentId}"]`);
                    handleAgentModelUpdate(agentId, input.value.trim());
                });
            });
        } catch (err) {
            console.error("Error loading agent registry:", err);
        }
    }

    async function handleAgentModelUpdate(agentId, model) {
        try {
            const res = await fetch("/api/registry/update", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ agent_id: agentId, model: model })
            });
            const data = await res.json();
            if (data.success) {
                loadAgentRegistry();
                appendMessage("system", `Updated agent override: Set '${agentId}' model to '${model || 'DEFAULT'}'`);
            } else {
                alert(`Error: ${data.error}`);
            }
        } catch (err) {
            console.error(err);
        }
    }

    async function loadAgentRules() {
        try {
            const res = await fetch("/api/registry/rules");
            const data = await res.json();
            const tbody = document.querySelector("#rules-table tbody");
            if (!tbody) return;
            tbody.innerHTML = "";

            data.forEach(rule => {
                const tr = document.createElement("tr");
                tr.innerHTML = `
                    <td><code>${rule.agent_id}</code></td>
                    <td><strong>${rule.key}</strong></td>
                    <td>${rule.text}</td>
                    <td>
                        <input type="checkbox" class="rule-active-toggle" data-key="${rule.key}" ${rule.is_active ? "checked" : ""}>
                    </td>
                    <td>
                        <button class="btn btn-secondary btn-delete-rule" data-key="${rule.key}">&times;</button>
                    </td>
                `;
                tbody.appendChild(tr);
            });

            // Bind toggle change event
            document.querySelectorAll(".rule-active-toggle").forEach(cb => {
                cb.addEventListener("change", (e) => {
                    const ruleKey = cb.getAttribute("data-key");
                    handleAgentRuleToggle(ruleKey, cb.checked);
                });
            });

            // Bind delete rule event
            document.querySelectorAll(".btn-delete-rule").forEach(btn => {
                btn.addEventListener("click", () => {
                    const ruleKey = btn.getAttribute("data-key");
                    if (confirm(`Are you sure you want to delete rule '${ruleKey}'?`)) {
                        handleAgentRuleDelete(ruleKey);
                    }
                });
            });
        } catch (err) {
            console.error("Error loading agent rules:", err);
        }
    }

    async function handleAgentRuleAdd() {
        const agentSelect = document.getElementById("rule-agent-select");
        const keyInput = document.getElementById("rule-key-input");
        const textInput = document.getElementById("rule-text-input");

        const agent_id = agentSelect.value;
        const rule_key = keyInput.value.trim().toLowerCase();
        const rule_text = textInput.value.trim();

        if (!rule_key || !rule_text) return;
        keyInput.value = "";
        textInput.value = "";

        try {
            const res = await fetch("/api/registry/rules/update", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ action: "add", agent_id: agent_id, rule_key: rule_key, rule_text: rule_text })
            });
            const data = await res.json();
            if (data.success) {
                loadAgentRules();
                appendMessage("system", `Added agent rule: [${agent_id}] '${rule_key}'`);
            } else {
                alert(`Error: ${data.error}`);
            }
        } catch (err) {
            console.error(err);
        }
    }

    async function handleAgentRuleToggle(ruleKey, isActive) {
        try {
            const res = await fetch("/api/registry/rules/update", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ action: "toggle", rule_key: ruleKey, is_active: isActive })
            });
            const data = await res.json();
            if (data.success) {
                loadAgentRules();
                const status = isActive ? "enabled" : "disabled";
                appendMessage("system", `Agent rule '${ruleKey}' ${status}`);
            } else {
                alert(`Error: ${data.error}`);
            }
        } catch (err) {
            console.error(err);
        }
    }

    async function handleAgentRuleDelete(ruleKey) {
        try {
            const res = await fetch("/api/registry/rules/update", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ action: "delete", rule_key: ruleKey })
            });
            const data = await res.json();
            if (data.success) {
                loadAgentRules();
                appendMessage("system", `Deleted agent rule '${ruleKey}'`);
            } else {
                alert(`Error: ${data.error}`);
            }
        } catch (err) {
            console.error(err);
        }
    }

    // --- Modal Functions ---

    function openModal(delib) {
        const isApproved = delib.decision === 1;
        const verdictLabel = isApproved ? "Approved" : "Vetoed";
        const verdictClass = isApproved ? "approved" : "vetoed";

        modalActionName.textContent = delib.action;
        modalCriticVerdict.textContent = verdictLabel;
        modalCriticVerdict.className = `meta-val badge ${verdictClass}`;
        modalUtilityScore.textContent = delib.utility;
        modalCriticJustification.textContent = delib.justification;
        
        modalRawJson.textContent = JSON.stringify(delib.debate, null, 2);

        auditModal.classList.add("active");
    }

    function closeModal() {
        auditModal.classList.remove("active");
    }
});
