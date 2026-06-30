/**
 * NeMo Sortformer режим — JS такой же как у pyannote (общая логика модалки/прогресса),
 * но передаётся mode=nemo, и для задач показывается отдельный список.
 *
 * В этом режиме post-processing (voting/merge/profiles) НЕ применяется — модель
 * сама даёт чистые сегменты.
 */

async function apiFetch(url, opts = {}) {
    const r = await fetch(url, opts);
    if (r.status === 401) {
        window.location.href = "/login";
        throw new Error("Сессия истекла");
    }
    return r;
}

const API = {
    health: () => apiFetch("/health").then(r => r.json()),
    list:   () => apiFetch("/api/jobs?mode=nemo-sortformer").then(r => r.json()),
    get:    (id) => apiFetch(`/api/jobs/${id}`).then(r => r.json()),
    upload: (formData) => window.resumableUpload(formData),
    rename: (id, mapping) =>
        apiFetch(`/api/jobs/${id}/speakers`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(mapping),
        }).then(async r => {
            if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
            return r.json();
        }),
    remove: (id) => apiFetch(`/api/jobs/${id}`, { method: "DELETE" }).then(r => r.json()),
    download: (id) => { window.location.href = `/api/jobs/${id}/download`; },
    logout: () => apiFetch("/api/logout", { method: "POST" }).then(r => r.json()),
};

const STATUS_LABEL = {
    pending: "В очереди",
    running: "Обработка",
    done: "Готово",
    failed: "Ошибка",
};

// ----- Upload -----

const dropZone = document.getElementById("drop-zone");
const fileInput = document.getElementById("file-input");

dropZone.addEventListener("click", () => fileInput.click());
dropZone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); }
});
["dragenter", "dragover"].forEach(ev =>
    dropZone.addEventListener(ev, (e) => { e.preventDefault(); dropZone.classList.add("drag-over"); })
);
["dragleave", "drop"].forEach(ev =>
    dropZone.addEventListener(ev, (e) => { e.preventDefault(); dropZone.classList.remove("drag-over"); })
);
dropZone.addEventListener("drop", (e) => {
    const files = e.dataTransfer?.files;
    if (files && files.length > 0) handleFiles(files);
});
fileInput.addEventListener("change", (e) => {
    if (e.target.files?.length > 0) {
        handleFiles(e.target.files);
        e.target.value = "";
    }
});

// ----- Предупреждение о лимите 4 спикеров -----

const SORTFORMER_MAX_SPEAKERS = 4;

function refreshSpeakerWarning() {
    const count = window.countParticipants ? window.countParticipants() : 0;
    const warn = document.getElementById("speaker-warning");
    const warnCount = document.getElementById("speaker-warning-count");
    if (!warn) return;
    if (count > SORTFORMER_MAX_SPEAKERS) {
        if (warnCount) warnCount.textContent = String(count);
        warn.classList.remove("hidden");
    } else {
        warn.classList.add("hidden");
    }
}

document.addEventListener("participants:change", refreshSpeakerWarning);
document.addEventListener("DOMContentLoaded", refreshSpeakerWarning);

async function handleFiles(fileList) {
    const count = window.countParticipants ? window.countParticipants() : 0;
    const numSpeakers = count > 0 ? String(count) : "";
    const prompt = window.buildParticipantsPrompt ? window.buildParticipantsPrompt() : "";

    dropZone.classList.add("uploading");
    try {
        for (const file of fileList) {
            const fd = new FormData();
            fd.append("file", file);
            fd.append("language", "ru");
            fd.append("mode", "nemo-sortformer");
            if (numSpeakers) fd.append("num_speakers", numSpeakers);
            if (prompt) fd.append("initial_prompt", prompt);

            toast(`Загружаю «${file.name}»…`);
            try {
                await API.upload(fd);
                toast(`«${file.name}» поставлен в очередь`);
            } catch (err) {
                toast(`Ошибка: ${err.message}`, true);
            }
        }
        await refreshJobs();
    } finally {
        dropZone.classList.remove("uploading");
    }
}

// ----- Jobs list -----

const jobsList = document.getElementById("jobs-list");
document.getElementById("refresh-btn").addEventListener("click", refreshJobs);

async function refreshJobs() {
    try {
        const jobs = await API.list();
        renderJobs(jobs);
    } catch (err) {
        toast(`Не удалось загрузить список: ${err.message}`, true);
    }
}

function renderJobs(jobs) {
    if (!jobs || jobs.length === 0) {
        jobsList.innerHTML = `<div class="empty">Пока нет ни одной задачи. Загрузите аудиофайл выше.</div>`;
        return;
    }
    jobsList.innerHTML = jobs.map(j => {
        const pct = Math.round((j.progress || 0) * 100);
        const statusLabel = STATUS_LABEL[j.status] || j.status;
        const isDone = j.status === "done";
        const isFailed = j.status === "failed";
        const isActive = j.status === "running" || j.status === "pending";
        return `
            <div class="job" data-job-id="${j.id}">
                <div class="job-main">
                    <div class="job-name" title="${escapeHtml(j.filename)}">${escapeHtml(j.filename)}</div>
                    <div class="job-meta">
                        <span class="status-chip status-${j.status}">${statusLabel}</span>
                        ${j.stage && isActive ? `<span>${escapeHtml(j.stage)}…</span>` : ""}
                        <span>${formatTime(j.created_at)}</span>
                    </div>
                </div>
                <div class="job-actions">
                    ${isDone ? `<button class="primary-btn" data-action="open">Открыть</button>` : ""}
                    ${isDone ? `<button class="ghost-btn" data-action="download" title="Скачать .docx">⬇</button>` : ""}
                    ${isFailed ? `<button class="ghost-btn" data-action="open">Подробнее</button>` : ""}
                    <button class="danger-btn" data-action="delete" title="Удалить">✕</button>
                </div>
                <div class="progress ${isDone ? "done" : ""} ${isFailed ? "failed" : ""}">
                    <div class="progress-bar" style="width: ${isFailed ? 100 : pct}%"></div>
                </div>
            </div>
        `;
    }).join("");

    jobsList.querySelectorAll(".job").forEach(node => {
        const id = node.dataset.jobId;
        node.querySelectorAll("[data-action]").forEach(btn => {
            btn.addEventListener("click", (e) => {
                e.stopPropagation();
                const action = btn.dataset.action;
                if (action === "open") openJobModal(id);
                else if (action === "download") API.download(id);
                else if (action === "delete") confirmDelete(id);
            });
        });
    });
}

async function confirmDelete(id) {
    if (!confirm("Удалить задачу и связанные файлы?")) return;
    await API.remove(id);
    if (currentModalJobId === id) closeModal();
    refreshJobs();
    toast("Удалено");
}

function formatTime(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return d.toLocaleString("ru-RU", {
        day: "2-digit", month: "2-digit", year: "numeric",
        hour: "2-digit", minute: "2-digit",
    });
}
function formatTs(seconds) {
    const total = Math.floor(seconds);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}
function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ----- Modal -----

const modalBackdrop = document.getElementById("modal-backdrop");
const modalBody = document.getElementById("modal-body");
const modalTitle = document.getElementById("modal-title");
let currentModalJobId = null;
let currentModalStatus = null;

document.getElementById("modal-close").addEventListener("click", closeModal);
modalBackdrop.addEventListener("click", (e) => {
    if (e.target === modalBackdrop) closeModal();
});

async function openJobModal(jobId) {
    currentModalJobId = jobId;
    currentModalStatus = null;
    modalBackdrop.classList.remove("hidden");
    modalBody.innerHTML = `<div class="empty">Загрузка…</div>`;
    try {
        const job = await API.get(jobId);
        modalTitle.textContent = job.filename;
        currentModalStatus = job.status;
        renderModalBody(job);
    } catch (err) {
        modalBody.innerHTML = `<div class="error-box">${escapeHtml(err.message)}</div>`;
    }
}
function closeModal() {
    currentModalJobId = null;
    currentModalStatus = null;
    modalBackdrop.classList.add("hidden");
}

function renderModalBody(job) {
    if (job.status === "failed") {
        modalBody.innerHTML = `<div class="error-box">${escapeHtml(job.error || "Неизвестная ошибка")}</div>`;
        return;
    }
    if (job.status !== "done") {
        modalBody.innerHTML = `<div class="empty">Задача ещё обрабатывается…</div>`;
        return;
    }

    const segments = job.segments || [];
    // PAUSE — служебный маркер паузы, не участник: убираем из списка говорящих.
    const speakers = (job.speakers || []).filter(s => s !== "PAUSE");
    const names = job.speaker_names || {};

    const speakersEditor = speakers.length === 0
        ? ""
        : `
            <p style="color: var(--text-dim); font-size: 13px; margin-top: 0;">
                Сопоставьте говорящих с участниками — документ Word будет сформирован заново.
            </p>
            <div class="speakers-editor two-col">
                ${speakers.map(spk => `
                    <div class="speaker-label">${escapeHtml(speakerName(spk))}</div>
                    <input type="text" data-speaker="${escapeHtml(spk)}"
                           value="${escapeHtml(names[spk] || "")}"
                           placeholder="например, Судья Иванов И.И.">
                `).join("")}
            </div>
        `;

    const previewSegments = segments.slice(0, 80);
    const preview = previewSegments.length === 0
        ? `<div class="empty">Текст отсутствует.</div>`
        : `
            <h4 style="margin-top: 20px; margin-bottom: 8px; color: var(--text-dim); font-size: 13px; text-transform: uppercase; letter-spacing: 0.04em;">
                Предпросмотр (${previewSegments.length}${segments.length > previewSegments.length ? ` из ${segments.length}` : ""} реплик)
            </h4>
            <div class="transcript-preview">
                ${previewSegments.map(seg => {
                    if (seg.speaker === "PAUSE") {
                        return `<div class="turn"><span class="ts">[${formatTs(seg.start)}]</span>
                                <em style="color:var(--text-dim);">${escapeHtml(seg.text)}</em></div>`;
                    }
                    const displayName = names[seg.speaker] || speakerName(seg.speaker);
                    return `<div class="turn">
                        <span class="ts">[${formatTs(seg.start)}]</span>
                        <span class="speaker">${escapeHtml(displayName)}:</span>
                        ${escapeHtml(seg.text)}
                    </div>`;
                }).join("")}
            </div>
        `;

    modalBody.innerHTML = `
        ${speakersEditor}
        ${preview}
        <div class="modal-footer">
            <button class="ghost-btn" id="modal-cancel">Закрыть</button>
            ${speakers.length > 0 ? `<button class="primary-btn" id="modal-save">Сохранить имена</button>` : ""}
            <button class="primary-btn" id="modal-download">Скачать .docx</button>
        </div>
    `;

    document.getElementById("modal-cancel").addEventListener("click", closeModal);
    document.getElementById("modal-download").addEventListener("click", () => API.download(job.id));
    const saveBtn = document.getElementById("modal-save");
    if (saveBtn) {
        saveBtn.addEventListener("click", async () => {
            const mapping = {};
            modalBody.querySelectorAll("input[data-speaker]").forEach(inp => {
                const val = inp.value.trim();
                if (val) mapping[inp.dataset.speaker] = val;
            });
            saveBtn.disabled = true;
            try {
                await API.rename(job.id, mapping);
                toast("Имена сохранены, документ сформирован заново.");
                openJobModal(job.id);
            } catch (err) {
                toast(`Ошибка: ${err.message}`, true);
            } finally {
                saveBtn.disabled = false;
            }
        });
    }
}

// ----- Toast -----

const toastEl = document.getElementById("toast");
let toastTimer = null;
function toast(msg, isError = false) {
    toastEl.textContent = msg;
    toastEl.classList.toggle("error", isError);
    toastEl.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.add("hidden"), 3500);
}

// ----- Bootstrap + polling -----

async function loadHealth() {
    try {
        const h = await API.health();
        document.getElementById("backend-badge").textContent = "● На связи";
        if (h.auth_enabled) {
            document.getElementById("logout-btn").classList.remove("hidden");
        }
    } catch {
        document.getElementById("backend-badge").textContent = "● Нет связи";
    }
}

document.getElementById("logout-btn").addEventListener("click", async () => {
    await API.logout();
    window.location.href = "/login";
});

loadHealth();
refreshJobs();

setInterval(async () => {
    try {
        const jobs = await API.list();
        const hasActive = jobs.some(j => j.status === "pending" || j.status === "running");
        if (hasActive || jobsList.querySelector(".job .progress-bar")) {
            renderJobs(jobs);
        }
        if (currentModalJobId) {
            const j = await API.get(currentModalJobId);
            if (j.status !== currentModalStatus) {
                currentModalStatus = j.status;
                renderModalBody(j);
            }
        }
    } catch { /* network blip */ }
}, 2000);
