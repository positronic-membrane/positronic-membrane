/* ==============================================================================
   PROJECT JANUS: CLIENT-SIDE APP LOGIC
   ============================================================================== */

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
    init();

    function init() {
        // Load initial chat history
        loadChatHistory();

        // Load initial deliberations and start polling
        loadDeliberations();
        setInterval(loadDeliberations, 4000); // Poll every 4 seconds

        // Event Listeners
        chatForm.addEventListener("submit", handleChatSubmit);
        closeModalBtn.addEventListener("click", () => closeModal());
        
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
            const data = await res.json();

            // Remove typing indicator
            indicator.remove();

            if (data.error) {
                appendMessage("persona", `*(System Error: ${data.error})*`);
            } else if (data.response) {
                appendMessage("persona", data.response);
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
        // Escaped HTML to prevent XSS injection
        let escaped = text
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;");

        // Format code blocks: `code`
        escaped = escaped.replace(/`([^`]+)`/g, "<code>$1</code>");

        // Format bullet points (simple markdown conversion)
        // Match "- " or "* " at the start of a line and group
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

            // Check if we actually have any new elements to redraw
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
            
            // Format Timestamp (just get local HH:MM:SS)
            let timeStr = d.timestamp;
            try {
                // If timestamp contains space, split to get time part
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

            // Click listener to inspect details
            card.addEventListener("click", () => openModal(d));

            deliberationsTimeline.appendChild(card);
        });
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
        
        // Render pretty formatted raw deliberation debate JSON
        modalRawJson.textContent = JSON.stringify(delib.debate, null, 2);

        auditModal.classList.add("active");
    }

    function closeModal() {
        auditModal.classList.remove("active");
    }
});
