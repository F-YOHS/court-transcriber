/**
 * Общий модуль «Состав участников».
 *
 * UI: фиксированные роли (только поле имени) + динамические роли (роль + имя + удалить).
 * Состояние сохраняется в localStorage, чтобы не вводить заново при каждой загрузке.
 *
 * API для остальных JS:
 *   window.collectParticipants()     → массив {role, name}
 *   window.buildParticipantsPrompt() → строка для initial_prompt
 *   window.countParticipants()       → число (для num_speakers)
 */

const FIXED_ROLES = [
    { key: "judge",       label: "Председательствующий судья" },
    { key: "prosecutor",  label: "Государственный обвинитель (прокурор)" },
    { key: "defender",    label: "Защитник (адвокат)" },
    { key: "defendant",   label: "Подсудимый / обвиняемый" },
];

const STORAGE_KEY = "court-transcriber-participants-v1";

function loadState() {
    try {
        const raw = localStorage.getItem(STORAGE_KEY);
        if (raw) {
            const parsed = JSON.parse(raw);
            return {
                fixed: parsed.fixed || {},
                dynamic: Array.isArray(parsed.dynamic) ? parsed.dynamic : [],
            };
        }
    } catch {}
    return { fixed: {}, dynamic: [] };
}

function saveState(state) {
    try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    } catch {}
}

let state = loadState();

function _escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function updateCount() {
    const count = countParticipants();
    document.querySelectorAll(".participants-count").forEach(el => {
        el.textContent = `(${count})`;
    });
    // Кастомное событие — страница может среагировать (например, показать
    // предупреждение о превышении лимита спикеров).
    try {
        document.dispatchEvent(new CustomEvent("participants:change", { detail: { count } }));
    } catch {}
}

function renderFixed() {
    const root = document.getElementById("fixed-roles");
    if (!root) return;
    root.innerHTML = FIXED_ROLES.map(r => `
        <div class="participant-row">
            <div class="participant-role-label">${r.label}</div>
            <input type="text" class="participant-name-input"
                   data-role-key="${r.key}"
                   value="${_escapeHtml(state.fixed[r.key] || "")}"
                   placeholder="Имя, необязательно (например: Иванов И.И.)">
        </div>
    `).join("");

    root.querySelectorAll(".participant-name-input").forEach(inp => {
        inp.addEventListener("input", () => {
            state.fixed[inp.dataset.roleKey] = inp.value;
            saveState(state);
        });
    });
}

function renderDynamic() {
    const root = document.getElementById("dynamic-roles");
    if (!root) return;
    if (state.dynamic.length === 0) {
        root.innerHTML = `<div class="empty-roles">Пока нет дополнительных участников. Нажмите «+ Добавить» если в заседании есть свидетель, секретарь, эксперт и т.д.</div>`;
        return;
    }
    root.innerHTML = state.dynamic.map((p, idx) => `
        <div class="participant-row dynamic">
            <input type="text" class="participant-role-input"
                   data-idx="${idx}"
                   value="${_escapeHtml(p.role || "")}"
                   placeholder="Роль (например: свидетель Иванова)">
            <input type="text" class="participant-name-input dynamic"
                   data-idx="${idx}"
                   value="${_escapeHtml(p.name || "")}"
                   placeholder="Имя, необязательно">
            <button type="button" class="participant-remove" data-idx="${idx}" title="Удалить">✕</button>
        </div>
    `).join("");

    root.querySelectorAll(".participant-role-input").forEach(inp => {
        inp.addEventListener("input", () => {
            state.dynamic[+inp.dataset.idx].role = inp.value;
            saveState(state);
        });
    });
    root.querySelectorAll(".participant-name-input.dynamic").forEach(inp => {
        inp.addEventListener("input", () => {
            state.dynamic[+inp.dataset.idx].name = inp.value;
            saveState(state);
        });
    });
    root.querySelectorAll(".participant-remove").forEach(btn => {
        btn.addEventListener("click", () => {
            state.dynamic.splice(+btn.dataset.idx, 1);
            saveState(state);
            renderDynamic();
            updateCount();
        });
    });
}

function addParticipant() {
    state.dynamic.push({ role: "", name: "" });
    saveState(state);
    renderDynamic();
    updateCount();
    // Фокусируем поле роли в только что добавленной строке
    setTimeout(() => {
        const inputs = document.querySelectorAll(".participant-role-input");
        if (inputs.length > 0) inputs[inputs.length - 1].focus();
    }, 0);
}

function collectParticipants() {
    const out = [];
    FIXED_ROLES.forEach(r => {
        out.push({
            role: r.label,
            name: (state.fixed[r.key] || "").trim(),
        });
    });
    state.dynamic.forEach(p => {
        const role = (p.role || "").trim();
        if (role) {
            out.push({ role, name: (p.name || "").trim() });
        }
    });
    return out;
}

function buildParticipantsPrompt() {
    const all = collectParticipants();
    if (all.length === 0) return "";
    return all.map(p => (p.name ? `${p.role} ${p.name}` : p.role)).join(", ") + ".";
}

function countParticipants() {
    return collectParticipants().length;
}

// "SPEAKER_00" → "Говорящий 1" и т.д. Именованные/прочие значения — как есть.
function speakerName(spk) {
    const m = /^SPEAKER_(\d+)$/.exec(String(spk));
    return m ? `Говорящий ${parseInt(m[1], 10) + 1}` : spk;
}

// Публичный API
window.collectParticipants = collectParticipants;
window.buildParticipantsPrompt = buildParticipantsPrompt;
window.countParticipants = countParticipants;
window.speakerName = speakerName;

// --- Bootstrap ---

document.addEventListener("DOMContentLoaded", () => {
    renderFixed();
    renderDynamic();
    updateCount();

    document.getElementById("open-participants")?.addEventListener("click", () => {
        document.getElementById("participants-modal")?.classList.remove("hidden");
    });
    document.querySelectorAll('[data-close="participants-modal"]').forEach(btn => {
        btn.addEventListener("click", () => {
            document.getElementById("participants-modal")?.classList.add("hidden");
        });
    });
    document.getElementById("participants-modal")?.addEventListener("click", (e) => {
        if (e.target.id === "participants-modal") {
            e.target.classList.add("hidden");
        }
    });
    document.getElementById("add-role")?.addEventListener("click", addParticipant);
});
